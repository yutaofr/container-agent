// Package inspect reads what the platform needs to draft a descriptor for a project that has
// none: build files, Dockerfiles, Spring configuration, and existing descriptors. It reads a
// fixed set of file names in the project root and in the modules the project's own build
// files list; the platform names only the project directory, never a file. Init resources
// (SQL, CQL, migrations) are reported by path only.
package inspect

import (
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"slices"
	"strings"

	"github.com/yutaofr/container-agent/runner/internal/guard"
	"github.com/yutaofr/container-agent/runner/internal/redact"
)

// Limits on what one inspection reads and reports.
const (
	MaxModules    = 50
	MaxFileBytes  = 64 << 10
	MaxTotalBytes = 2 << 20
	MaxResources  = 200
	maxWalk       = 50_000 // directory entries visited while looking for resources
)

// ModuleFiles are read in the root and in every module, relative to that directory.
var ModuleFiles = []string{
	"pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
	"package.json", "go.mod", "Dockerfile",
	"src/main/resources/application.yml", "src/main/resources/application.yaml",
	"src/main/resources/application.properties",
}

// Descriptor files are read in the root only.
const (
	descriptorFile = ".cagent/service.yaml"
	descriptorDir  = ".cagent/services"
)

// skipDirs are never searched for resources: dependencies, build output, tool state.
var skipDirs = map[string]bool{
	".git": true, ".gradle": true, ".idea": true, ".cagent": true,
	"node_modules": true, "target": true, "build": true, "dist": true, "vendor": true,
}

// Result is what inspect reports. Paths are relative to the project root, with slashes.
type Result struct {
	Modules   []string          `json:"modules"`   // module directories, in discovery order
	Files     map[string]string `json:"files"`     // path -> redacted content
	Resources []string          `json:"resources"` // init-resource files, sorted
	Truncated []string          `json:"truncated"` // cut files, files over the total, "modules", "resources"
}

type reader struct {
	root  string
	total int
	res   Result
}

// Inspect reads dir. dir must be a directory; the caller checks it is one the user allowed.
func Inspect(dir string) (Result, error) {
	root, err := guard.Resolve(dir)
	if err != nil {
		return Result{}, err
	}
	if fi, err := os.Stat(root); err != nil || !fi.IsDir() {
		return Result{}, fmt.Errorf("%s is not a directory", dir)
	}
	r := &reader{root: root, res: Result{
		Modules: []string{}, Files: map[string]string{}, Resources: []string{}, Truncated: []string{},
	}}
	r.readDescriptors()
	seen := map[string]bool{".": true}
	queue := []string{"."}
	for len(queue) > 0 {
		mod := queue[0]
		queue = queue[1:]
		for _, child := range r.readModule(mod) {
			if seen[child] {
				continue
			}
			seen[child] = true
			if len(r.res.Modules) == MaxModules {
				r.truncated("modules")
				continue
			}
			r.res.Modules = append(r.res.Modules, child)
			queue = append(queue, child)
		}
	}
	r.findResources()
	return r.res, nil
}

func (r *reader) truncated(what string) {
	if !slices.Contains(r.res.Truncated, what) {
		r.res.Truncated = append(r.res.Truncated, what)
	}
}

// inside resolves rel under the root and reports whether it stays there.
func (r *reader) inside(rel string) (string, bool) {
	p, err := guard.Resolve(filepath.Join(r.root, filepath.FromSlash(rel)))
	if err != nil || !guard.Within(r.root, p) {
		return "", false
	}
	return p, true
}

// read reads one file into the result and returns its raw (unredacted) content. Missing
// files, non-regular files, and symlinks leaving the root are skipped.
func (r *reader) read(rel string) (string, bool) {
	p, ok := r.inside(rel)
	if !ok {
		return "", false
	}
	fi, err := os.Stat(p)
	if err != nil || !fi.Mode().IsRegular() {
		return "", false
	}
	budget := min(MaxFileBytes, MaxTotalBytes-r.total)
	if budget <= 0 {
		r.truncated(rel)
		return "", false
	}
	f, err := os.Open(p)
	if err != nil {
		return "", false
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, int64(budget)+1))
	if err != nil {
		return "", false
	}
	if len(data) > budget {
		data = data[:budget]
		r.truncated(rel)
	}
	r.total += len(data)
	raw := string(data)
	r.res.Files[rel] = redact.String(raw)
	return raw, true
}

func (r *reader) readDescriptors() {
	r.read(descriptorFile)
	dir, ok := r.inside(descriptorDir)
	if !ok {
		return
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".yaml") {
			r.read(descriptorDir + "/" + e.Name())
		}
	}
}

// readModule reads a module's files and returns the child modules its build files list.
func (r *reader) readModule(mod string) []string {
	var children []string
	add := func(rel string) {
		child := path.Join(mod, rel)
		if !relative(rel) || child == "." {
			return
		}
		if p, ok := r.inside(child); ok {
			if fi, err := os.Stat(p); err == nil && fi.IsDir() {
				children = append(children, child)
			}
		}
	}
	for _, name := range ModuleFiles {
		raw, ok := r.read(path.Join(mod, name))
		if !ok {
			continue
		}
		switch name {
		case "pom.xml":
			for _, m := range pomModules(raw) {
				add(m)
			}
		case "settings.gradle", "settings.gradle.kts":
			for _, m := range gradleIncludes(raw) {
				add(m)
			}
		}
	}
	return children
}

func relative(p string) bool {
	if p == "" || path.IsAbs(p) || filepath.IsAbs(p) {
		return false
	}
	c := path.Clean(p)
	return c != ".." && !strings.HasPrefix(c, "../")
}

func pomModules(raw string) []string {
	var p struct {
		Modules []string `xml:"modules>module"`
	}
	if err := xml.Unmarshal([]byte(raw), &p); err != nil {
		return nil
	}
	var out []string
	for _, m := range p.Modules {
		out = append(out, strings.TrimSpace(m))
	}
	return out
}

var (
	gradleInclude = regexp.MustCompile(`(?m)^\s*include\b(.*)$`)
	quoted        = regexp.MustCompile(`["']([^"']+)["']`)
)

// gradleIncludes maps `include("web", ":lib:core")` to web and lib/core.
func gradleIncludes(raw string) []string {
	var out []string
	for _, line := range gradleInclude.FindAllStringSubmatch(raw, -1) {
		for _, q := range quoted.FindAllStringSubmatch(line[1], -1) {
			out = append(out, strings.ReplaceAll(strings.TrimPrefix(q[1], ":"), ":", "/"))
		}
	}
	return out
}

// isResource reports whether a file is an init resource: CQL, schema SQL, SQL under db/init,
// or a Flyway or Liquibase file.
func isResource(rel string) bool {
	under := func(dir string) bool {
		return strings.HasPrefix(rel, dir+"/") || strings.Contains(rel, "/"+dir+"/")
	}
	base := path.Base(rel)
	schema, _ := path.Match("schema*.sql", base)
	return strings.HasSuffix(base, ".cql") || schema ||
		(under("db/init") && strings.HasSuffix(base, ".sql")) ||
		under("db/migration") || under("db/changelog")
}

var errStop = errors.New("stop")

func (r *reader) findResources() {
	visited := 0
	err := filepath.WalkDir(r.root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil // unreadable directory: skip it
		}
		if visited++; visited > maxWalk {
			r.truncated("resources")
			return errStop
		}
		if d.IsDir() {
			if p != r.root && skipDirs[d.Name()] {
				return filepath.SkipDir
			}
			return nil
		}
		rel, err := filepath.Rel(r.root, p)
		if err != nil || !d.Type().IsRegular() || !isResource(filepath.ToSlash(rel)) {
			return nil
		}
		if len(r.res.Resources) == MaxResources {
			r.truncated("resources")
			return errStop
		}
		r.res.Resources = append(r.res.Resources, filepath.ToSlash(rel))
		return nil
	})
	_ = err // errStop or nil: what was found so far is the answer
	slices.Sort(r.res.Resources)
}
