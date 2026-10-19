package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"sync"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
)

// executor runs tool calls one at a time for the whole process, across reconnects. A call
// belongs to the connection it arrived on: when that connection ends, its queued calls are
// dropped, and a call already running finishes but answers only on its own connection. So
// a reconnect never lets an old call run beside a new one, and the platform can ask
// (status) whether anything is still running.
type executor struct {
	cfg      serveConfig
	d        deps
	instance string // one per process, unchanged across reconnects
	queue    chan job

	mu     sync.Mutex
	gen    uint64 // generation of the live connection; 0 while disconnected
	last   uint64
	active *activeCall
	queued map[string]int // cluster -> calls waiting
}

type job struct {
	gen     uint64
	conn    *websocket.Conn
	ctx     context.Context // the connection's context
	req     rpcRequest
	cluster string
}

type activeCall struct {
	CallID    json.RawMessage `json:"call_id"`
	Method    string          `json:"method"`
	Cluster   string          `json:"cluster"`
	StartedAt time.Time       `json:"started_at"`
}

// runnerStatus is what the status tool reports.
type runnerStatus struct {
	InstanceID string         `json:"instance_id"`
	Active     *activeCall    `json:"active"`
	Queued     map[string]int `json:"queued"`
}

func newExecutor(cfg serveConfig, d deps) *executor {
	size := cfg.queueSize
	if size <= 0 {
		size = 64
	}
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return &executor{cfg: cfg, d: d, instance: hex.EncodeToString(b),
		queue: make(chan job, size), queued: map[string]int{}}
}

// connect makes a new connection generation the live one.
func (e *executor) connect() uint64 {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.last++
	e.gen = e.last
	return e.gen
}

// disconnect ends a generation: none of its queued calls will start.
func (e *executor) disconnect(gen uint64) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.gen == gen {
		e.gen = 0
	}
}

// enqueue queues a call; false means the queue is full.
func (e *executor) enqueue(j job) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	select {
	case e.queue <- j:
		e.queued[j.cluster]++
		return true
	default:
		return false
	}
}

func (e *executor) status() runnerStatus {
	e.mu.Lock()
	defer e.mu.Unlock()
	queued := make(map[string]int, len(e.queued))
	for c, n := range e.queued {
		queued[c] = n
	}
	var active *activeCall
	if e.active != nil {
		a := *e.active
		active = &a
	}
	return runnerStatus{InstanceID: e.instance, Active: active, Queued: queued}
}

// run works through the queue until ctx ends.
func (e *executor) run(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case j := <-e.queue:
			e.do(j)
		}
	}
}

func (e *executor) do(j job) {
	// Checking the generation and marking the call active happen under the lock that
	// disconnect takes, so a call cannot pass the check and then start after its
	// connection ended.
	e.mu.Lock()
	if e.queued[j.cluster]--; e.queued[j.cluster] <= 0 {
		delete(e.queued, j.cluster)
	}
	if j.gen != e.gen {
		e.mu.Unlock()
		return
	}
	e.active = &activeCall{CallID: j.req.ID, Method: j.req.Method, Cluster: j.cluster, StartedAt: time.Now().UTC()}
	e.mu.Unlock()

	// A started call runs to completion even if its connection drops meanwhile: stopping
	// a subcommand halfway would leave the cluster in a state nobody asked for.
	resp := dispatch(context.Background(), e.cfg, e.d, j.req)

	e.mu.Lock()
	e.active = nil
	e.mu.Unlock()
	if err := wsjson.Write(j.ctx, j.conn, resp); err != nil {
		j.conn.CloseNow() // the result is lost; the hub's call times out or fails
	}
}

// clusterOf reads the cluster a call targets, "" when it names none (inspect).
func clusterOf(params json.RawMessage) string {
	var p struct {
		Name string `json:"name"`
	}
	_ = json.Unmarshal(params, &p)
	return p.Name
}
