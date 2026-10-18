//go:build integration

package integration

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/yutaofr/container-agent/runner/internal/builder"
	"github.com/yutaofr/container-agent/runner/internal/inspect"
	"github.com/yutaofr/container-agent/runner/internal/workdir"
)

// A two-module project whose deployable module has its own Dockerfile. Its build command
// stands in for the developer's toolchain; no JDK or Node is needed.
var twoModules = map[string]string{
	"pom.xml":        "<project><modules><module>api</module><module>lib</module></modules></project>",
	"api/pom.xml":    "<project><artifactId>api</artifactId></project>",
	"lib/pom.xml":    "<project><artifactId>lib</artifactId></project>",
	"api/Dockerfile": "FROM busybox:1.37\nCOPY out/msg /msg\nCMD [\"cat\", \"/msg\"]\n",
	".cagent/services/api.yaml": "name: api\nkind: app\nstack: dockerfile\nsources:\n  workdir:\n" +
		"    build: 'mkdir -p api/out && echo built-by-the-project > api/out/msg'\n" +
		"    dockerfile: api/Dockerfile\n    context: api\nruntime: {}\n",
}

func TestMultiModuleProjectIsInspectedAndBuiltFromItsDockerfile(t *testing.T) {
	ctx := context.Background()
	proj := t.TempDir()
	for rel, body := range twoModules {
		path := filepath.Join(proj, rel)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	res, err := inspect.Inspect(proj)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(res.Modules, []string{"api", "lib"}) {
		t.Errorf("modules %v", res.Modules)
	}
	if _, ok := res.Files[".cagent/services/api.yaml"]; !ok {
		t.Errorf("descriptor not read back: %v", res.Files)
	}

	desc, err := workdir.LoadDescriptor(proj, "api")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := workdir.Build(ctx, proj, desc); err != nil {
		t.Fatal(err)
	}
	dockerfile, contextDir, err := workdir.DockerInputs(proj, desc)
	if err != nil {
		t.Fatal(err)
	}
	tag := uniqueTag(t, "workdir-dockerfile")
	if err := builder.BuildDockerfile(ctx, d, tag, dockerfile, contextDir); err != nil {
		t.Fatal(err)
	}
	out, err := exec.Command("docker", "run", "--rm", tag).CombinedOutput()
	if err != nil || strings.TrimSpace(string(out)) != "built-by-the-project" {
		t.Fatalf("run: %v: %s", err, out)
	}
}
