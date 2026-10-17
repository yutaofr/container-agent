package maven

import (
	"fmt"
	"regexp"
	"strings"
)

// Coord is a Maven artifact coordinate.
type Coord struct {
	Group, Artifact, Version, Ext string
}

// part allows the characters Maven coordinates use, and nothing that could form a path
// segment such as ".." or "/".
var part = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_.-]*$`)

// ParseCoord parses "group:artifact:version" or "group:artifact:version:ext" (default jar).
func ParseCoord(s string) (Coord, error) {
	fields := strings.Split(s, ":")
	if len(fields) != 3 && len(fields) != 4 {
		return Coord{}, fmt.Errorf("coordinate %q: want group:artifact:version[:ext]", s)
	}
	c := Coord{Group: fields[0], Artifact: fields[1], Version: fields[2], Ext: "jar"}
	if len(fields) == 4 {
		c.Ext = fields[3]
	}
	for _, f := range []string{c.Group, c.Artifact, c.Version, c.Ext} {
		if !part.MatchString(f) || strings.Contains(f, "..") {
			return Coord{}, fmt.Errorf("coordinate %q: invalid part %q", s, f)
		}
	}
	return c, nil
}

// Snapshot reports whether the version is a -SNAPSHOT that must be resolved.
func (c Coord) Snapshot() bool { return strings.HasSuffix(c.Version, "-SNAPSHOT") }

// dir is the coordinate's directory in the repository layout.
func (c Coord) dir() string {
	return strings.ReplaceAll(c.Group, ".", "/") + "/" + c.Artifact + "/" + c.Version
}

func (c Coord) String() string {
	return c.Group + ":" + c.Artifact + ":" + c.Version + ":" + c.Ext
}
