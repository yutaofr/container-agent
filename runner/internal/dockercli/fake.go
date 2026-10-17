package dockercli

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"time"
)

// Fake is a Docker for tests. It answers each command from Responses, keyed by the
// space-joined arguments, and records every call. An unknown command is an error.
type Fake struct {
	mu        sync.Mutex
	Responses map[string]FakeResponse
	Calls     []string
}

// FakeResponse is the canned result of one command.
type FakeResponse struct {
	Out   string
	Err   error
	Delay time.Duration // how long the command takes to answer
}

func (f *Fake) answer(args []string) ([]byte, error) {
	key := strings.Join(args, " ")
	f.mu.Lock()
	f.Calls = append(f.Calls, key)
	r, ok := f.Responses[key]
	f.mu.Unlock()
	if !ok {
		return nil, fmt.Errorf("fake docker: unexpected command %q", key)
	}
	time.Sleep(r.Delay)
	return []byte(r.Out), r.Err
}

func (f *Fake) Output(_ context.Context, args ...string) ([]byte, error) { return f.answer(args) }

func (f *Fake) Combined(_ context.Context, args ...string) ([]byte, error) { return f.answer(args) }
