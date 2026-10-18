package initscripts

import (
	"archive/zip"
	"errors"
	"path"
)

// bootClasses is where a Spring Boot jar keeps the application's classpath resources.
const bootClasses = "BOOT-INF/classes/"

// FromJar stages scripts from the jar the Runner downloaded into dest, replacing what dest
// held. A script path is a classpath path: BOOT-INF/classes/<path> is tried first, then
// <path>. Only regular file entries are read, at most MaxBytes each; nothing else in the
// jar is extracted.
func FromJar(jar string, scripts []Script, dest string) ([]Staged, error) {
	if err := Check(scripts); err != nil {
		return nil, err
	}
	if len(scripts) == 0 {
		return stage(nil, dest, nil) // clear what an earlier build staged; the jar is not read
	}
	zr, err := zip.OpenReader(jar)
	if err != nil {
		return nil, err
	}
	defer zr.Close()
	entries := map[string]*zip.File{}
	for _, f := range zr.File {
		if _, seen := entries[f.Name]; !seen {
			entries[f.Name] = f
		}
	}
	return stage(scripts, dest, func(s Script) ([]byte, error) {
		name := path.Clean(s.Path)
		f := entries[bootClasses+name]
		if f == nil {
			f = entries[name]
		}
		if f == nil {
			return nil, errors.New("not found in the jar")
		}
		if !f.Mode().IsRegular() {
			return nil, errors.New("not a regular file in the jar")
		}
		r, err := f.Open()
		if err != nil {
			return nil, err
		}
		defer r.Close()
		return readLimited(r)
	})
}
