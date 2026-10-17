package maven

import (
	"context"
	"crypto/sha1"
	"encoding/hex"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"

	"github.com/yutaofr/container-agent/runner/internal/workspace"
)

// Client fetches artifacts from one repository.
type Client struct {
	Repo Repo
	HTTP *http.Client
}

// Artifact is a coordinate resolved to a concrete version and download URL.
type Artifact struct {
	Coord    Coord
	Resolved string // e.g. 1.4.2, or 2.0-20261017.083000-3 for a snapshot
	URL      string
}

type metadata struct {
	Snapshot struct {
		Timestamp   string `xml:"timestamp"`
		BuildNumber string `xml:"buildNumber"`
	} `xml:"versioning>snapshot"`
	SnapshotVersions []struct {
		Classifier string `xml:"classifier"`
		Extension  string `xml:"extension"`
		Value      string `xml:"value"`
	} `xml:"versioning>snapshotVersions>snapshotVersion"`
}

// Resolve turns a coordinate into a download URL. A -SNAPSHOT version is resolved to its
// latest timestamped build through maven-metadata.xml, so the exact artifact is recorded.
func (c Client) Resolve(ctx context.Context, coord Coord) (Artifact, error) {
	base := c.Repo.URL + "/" + coord.dir()
	resolved := coord.Version
	if coord.Snapshot() {
		body, err := c.get(ctx, base+"/maven-metadata.xml")
		if err != nil {
			return Artifact{}, err
		}
		defer body.Close()
		var md metadata
		if err := xml.NewDecoder(body).Decode(&md); err != nil {
			return Artifact{}, fmt.Errorf("parse snapshot metadata for %s: %w", coord, err)
		}
		if resolved = snapshotValue(md, coord); resolved == "" {
			return Artifact{}, fmt.Errorf("%s: snapshot metadata names no %s build", coord, coord.Ext)
		}
	}
	url := fmt.Sprintf("%s/%s-%s.%s", base, coord.Artifact, resolved, coord.Ext)
	return Artifact{Coord: coord, Resolved: resolved, URL: url}, nil
}

func snapshotValue(md metadata, coord Coord) string {
	for _, v := range md.SnapshotVersions {
		if v.Extension == coord.Ext && v.Classifier == "" {
			return v.Value
		}
	}
	if md.Snapshot.Timestamp != "" && md.Snapshot.BuildNumber != "" {
		base := strings.TrimSuffix(coord.Version, "-SNAPSHOT")
		return base + "-" + md.Snapshot.Timestamp + "-" + md.Snapshot.BuildNumber
	}
	return ""
}

// Download writes the artifact to file inside dir and returns its path and SHA-256. When
// the repository publishes a .sha1 next to the artifact, the download must match it.
func (c Client) Download(ctx context.Context, a Artifact, dir, file string) (string, string, error) {
	body, err := c.get(ctx, a.URL)
	if err != nil {
		return "", "", err
	}
	defer body.Close()
	h := sha1.New()
	path, sum, err := workspace.WriteNoFollow(dir, file, io.TeeReader(body, h))
	if err != nil {
		return "", "", err
	}
	want, err := c.publishedSHA1(ctx, a.URL)
	if err != nil {
		os.Remove(path)
		return "", "", err
	}
	if got := hex.EncodeToString(h.Sum(nil)); want != "" && want != got {
		os.Remove(path)
		return "", "", fmt.Errorf("%s: sha1 %s does not match published %s", a.URL, got, want)
	}
	return path, sum, nil
}

// publishedSHA1 returns the repository's .sha1 for url, or "" when it has none.
func (c Client) publishedSHA1(ctx context.Context, url string) (string, error) {
	body, err := c.get(ctx, url+".sha1")
	if err != nil {
		var nf errNotFound
		if errors.As(err, &nf) {
			return "", nil
		}
		return "", err
	}
	defer body.Close()
	data, err := io.ReadAll(io.LimitReader(body, 1024))
	if err != nil {
		return "", err
	}
	// The file holds the hex digest, sometimes followed by the file name.
	fields := strings.Fields(string(data))
	if len(fields) == 0 {
		return "", fmt.Errorf("%s.sha1 is empty", url)
	}
	return strings.ToLower(fields[0]), nil
}

type errNotFound struct{ url string }

func (e errNotFound) Error() string { return e.url + ": not found" }

func (c Client) get(ctx context.Context, url string) (io.ReadCloser, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	if c.Repo.Username != "" {
		req.SetBasicAuth(c.Repo.Username, c.Repo.Password)
	}
	httpc := c.HTTP
	if httpc == nil {
		httpc = http.DefaultClient
	}
	resp, err := httpc.Do(req)
	if err != nil {
		return nil, err
	}
	switch {
	case resp.StatusCode == http.StatusOK:
		return resp.Body, nil
	case resp.StatusCode == http.StatusNotFound:
		resp.Body.Close()
		return nil, errNotFound{url}
	case resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden:
		resp.Body.Close()
		return nil, fmt.Errorf("%s: %s; check the <server> credentials for mirror %q", url, resp.Status, c.Repo.ID)
	default:
		resp.Body.Close()
		return nil, fmt.Errorf("%s: %s", url, resp.Status)
	}
}
