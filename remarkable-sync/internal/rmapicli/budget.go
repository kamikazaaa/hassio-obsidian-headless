package rmapicli

// Linux child resource limits are set in a private re-exec helper before rmapi
// starts. This changes only the child, never the long-lived converter process.
import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"
)

const childMarker = "REMARKABLE_INTERNAL_LIMITED_RMAPI"
const childLimit = "REMARKABLE_INTERNAL_FILE_LIMIT"
const maxFileBytes = 64 << 20
const maxWorkspaceBytes = 2 << 30

type acquisitionPolicy struct {
	FileBytes      uint64
	WorkspaceBytes int64
	Entries        int
	ReserveBytes   uint64
	Poll           time.Duration
}

var defaultPolicy = acquisitionPolicy{FileBytes: maxFileBytes, WorkspaceBytes: maxWorkspaceBytes, Entries: 65536, ReserveBytes: 256 << 20, Poll: 25 * time.Millisecond}

func init() {
	if os.Getenv(childMarker) != "1" {
		return
	}
	if len(os.Args) < 3 || os.Args[1] != "--remarkable-rmapi-child" {
		os.Exit(125)
	}
	limit, err := strconv.ParseUint(os.Getenv(childLimit), 10, 64)
	if err != nil || limit == 0 || limit > maxFileBytes {
		os.Exit(125)
	}
	syscall.Umask(0077)
	if syscall.Setrlimit(syscall.RLIMIT_FSIZE, &syscall.Rlimit{Cur: limit, Max: limit}) != nil {
		os.Exit(125)
	}
	if syscall.Setrlimit(syscall.RLIMIT_CORE, &syscall.Rlimit{Cur: 0, Max: 0}) != nil {
		os.Exit(125)
	}
	path, err := exec.LookPath(binary)
	if err != nil {
		os.Exit(125)
	}
	os.Unsetenv(childMarker)
	os.Unsetenv(childLimit)
	if syscall.Exec(path, append([]string{binary}, os.Args[2:]...), os.Environ()) != nil {
		os.Exit(125)
	}
}

func privateDirectory(path string) error {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return err
	}
	current := "/"
	for _, part := range strings.Split(strings.TrimPrefix(absolute, "/"), "/") {
		current = filepath.Join(current, part)
		info, err := os.Lstat(current)
		if os.IsNotExist(err) {
			if err := os.Mkdir(current, 0700); err != nil {
				return err
			}
			continue
		}
		if err != nil {
			return err
		}
		if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return errors.New("unsafe rmapi private directory")
		}
	}
	return nil
}
func measureWorkspace(root string, policy acquisitionPolicy) (int64, error) {
	var total int64
	count := 0
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if os.IsNotExist(err) {
			return nil
		}
		if err != nil {
			return err
		}
		count++
		if count > policy.Entries {
			return errors.New("rmapi workspace entry limit exceeded")
		}
		if d.Type()&os.ModeSymlink != 0 {
			return errors.New("rmapi workspace symlink forbidden")
		}
		if d.IsDir() {
			return nil
		}
		info, err := d.Info()
		if os.IsNotExist(err) {
			return nil
		}
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return errors.New("rmapi workspace special file forbidden")
		}
		if stat, ok := info.Sys().(*syscall.Stat_t); ok && stat.Nlink != 1 {
			return errors.New("rmapi workspace hardlink forbidden")
		}
		if info.Size() < 0 || uint64(info.Size()) > policy.FileBytes {
			return errors.New("rmapi file size limit exceeded")
		}
		total += info.Size()
		if total > policy.WorkspaceBytes {
			return errors.New("rmapi workspace byte limit exceeded")
		}
		return nil
	})
	return total, err
}
func availableBytes(root string) (uint64, error) {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(root, &stat); err != nil {
		return 0, err
	}
	return stat.Bavail * uint64(stat.Bsize), nil
}
func checkBudget(root string, policy acquisitionPolicy, initial bool) error {
	total, err := measureWorkspace(root, policy)
	if err != nil {
		return err
	}
	free, err := availableBytes(root)
	if err != nil {
		return err
	}
	needed := policy.ReserveBytes
	if initial {
		remaining := uint64(policy.WorkspaceBytes - total)
		if remaining > ^uint64(0)-needed {
			return errors.New("invalid rmapi free-space budget")
		}
		needed += remaining
	}
	if free < needed {
		return errors.New("insufficient free space for rmapi acquisition reserve")
	}
	return nil
}

type stderrFlag struct{ seen atomic.Bool }

func (w *stderrFlag) Write(p []byte) (int, error) {
	if len(p) > 0 {
		w.seen.Store(true)
	}
	return len(p), nil
}

func runLimited(ctx context.Context, workspace string, policy acquisitionPolicy, args ...string) error {
	if policy.FileBytes == 0 || policy.FileBytes > maxFileBytes || policy.WorkspaceBytes < 0 || policy.Entries < 1 || policy.Poll <= 0 {
		return errors.New("invalid acquisition policy")
	}
	if err := privateDirectory(workspace); err != nil {
		return err
	}
	for _, name := range []string{"client-home", "client-tmp", "client-cache"} {
		if err := privateDirectory(filepath.Join(workspace, name)); err != nil {
			return err
		}
	}
	if err := checkBudget(workspace, policy, true); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	self, err := os.Executable()
	if err != nil {
		return err
	}
	childArgs := append([]string{"--remarkable-rmapi-child"}, args...)
	cmd := exec.Command(self, childArgs...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGKILL}
	cmd.Env = []string{"PATH=" + os.Getenv("PATH"), "RMAPI_CONFIG=" + configPath(), "HOME=" + filepath.Join(workspace, "client-home"), "TMPDIR=" + filepath.Join(workspace, "client-tmp"), "XDG_CACHE_HOME=" + filepath.Join(workspace, "client-cache"), childMarker + "=1", childLimit + "=" + strconv.FormatUint(policy.FileBytes, 10)}
	var stderr stderrFlag
	cmd.Stderr = &stderr
	cmd.WaitDelay = 2 * time.Second
	// Linux parent-death signals attach to the creating OS thread. Keep it
	// alive until the child/group has finished, and kill actual rmapi if the
	// converter is abruptly terminated before its context cleanup can run.
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	if err := cmd.Start(); err != nil {
		return err
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	ticker := time.NewTicker(policy.Poll)
	defer ticker.Stop()
	killGroup := func() { _ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) }
	stopTimeout := context.AfterFunc(ctx, killGroup)
	defer stopTimeout()
	finish := func(cause error) error {
		killGroup()
		select {
		case waitErr := <-done:
			return errors.Join(cause, waitErr)
		case <-time.After(3 * time.Second):
			return errors.Join(cause, errors.New("rmapi process group did not finish"))
		}
	}
	for {
		select {
		case <-ctx.Done():
			return finish(ctx.Err())
		case <-ticker.C:
			if err := checkBudget(workspace, policy, false); err != nil {
				return finish(err)
			}
		case err := <-done:
			// Descendants may outlive a shell/wrapper even after its leader exits.
			killGroup()
			if err != nil {
				return fmt.Errorf("rmapi command failed: %w", err)
			}
			if err := checkBudget(workspace, policy, false); err != nil {
				return err
			}
			if stderr.seen.Load() {
				return errors.New("rmapi reported an acquisition error (details suppressed)")
			}
			return nil
		}
	}
}
