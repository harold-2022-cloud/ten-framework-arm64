//go:build linux || darwin
// +build linux darwin

package internal

import (
	"os/exec"
	"syscall"
	"testing"
)

// A worker being stopped must not take a newer worker's place on the list.
// The meeting app uses one channel for every meeting: when the reaper stops
// an idle worker, its port can close a moment before its process is gone,
// a phone then starts a new worker on the same channel, and the old one's
// stop used to remove the channel -- the new worker's entry -- leaving that
// worker running, holding the port, and never reaped.
func TestStoppingAnOldWorkerLeavesTheNewOneOnTheList(t *testing.T) {
	const channel = "test-channel-reused"
	defer workers.Remove(channel)

	sleeper := exec.Command("sleep", "30")
	sleeper.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := sleeper.Start(); err != nil {
		t.Fatalf("start a stand-in worker: %v", err)
	}
	go func() { _ = sleeper.Wait() }()

	old := &Worker{ChannelName: channel, Pid: sleeper.Process.Pid}
	fresh := &Worker{ChannelName: channel}
	workers.Set(channel, fresh)

	if err := old.stop("test", channel); err != nil {
		t.Fatalf("stop: %v", err)
	}

	if got := workers.Get(channel); got != fresh {
		t.Fatalf("the new worker's entry is gone: %v", got)
	}
}

func TestStoppingAWorkerTakesItOffTheList(t *testing.T) {
	const channel = "test-channel-alone"
	defer workers.Remove(channel)

	sleeper := exec.Command("sleep", "30")
	sleeper.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := sleeper.Start(); err != nil {
		t.Fatalf("start a stand-in worker: %v", err)
	}
	go func() { _ = sleeper.Wait() }()

	w := &Worker{ChannelName: channel, Pid: sleeper.Process.Pid}
	workers.Set(channel, w)

	if err := w.stop("test", channel); err != nil {
		t.Fatalf("stop: %v", err)
	}

	if workers.Contains(channel) {
		t.Fatalf("the stopped worker is still on the list")
	}
}
