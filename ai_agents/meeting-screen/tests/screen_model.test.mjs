//
// This file is part of TEN Framework, an open source project.
// Licensed under the Apache License, Version 2.0.
//
// screen_model.mjs: what the page draws, and how a poll, a pin and a lost
// connection change it.  node --test ai_agents/meeting-screen/tests/
import assert from "node:assert/strict";
import { test } from "node:test";
import * as m from "../meeting_screen/static/screen_model.mjs";

const record = {
  duration_s: 100,
  summary: "結論",
  topics: [
    {
      start_s: 0,
      end_s: 50,
      summary: "第一段",
      utterances: [
        { speaker: 1, start_s: 10, end_s: 20, text: "乙說" },
        { speaker: 0, start_s: 0, end_s: 5, text: "甲說" },
      ],
    },
    {
      start_s: 50,
      end_s: 100,
      summary: "",
      utterances: [{ speaker: 0, start_s: 1, end_s: 9, text: "甲又說" }],
    },
  ],
};

test("times read as m:ss, and h:mm:ss past an hour", () => {
  assert.equal(m.mmss(0), "0:00");
  assert.equal(m.mmss(72.9), "1:12");
  assert.equal(m.mmss(3725), "1:02:05");
  assert.equal(m.minutesText(45), "45 秒");
  assert.equal(m.minutesText(35 * 60 + 14), "35 分鐘");
});

test("rows sit at the topic's start plus their own, in order", () => {
  const rows = m.transcriptRows(record, true);
  assert.deepEqual(
    rows.map((r) => r.at),
    [0, 10, 51]
  );
  assert.deepEqual(
    rows.map((r) => r.speaker),
    [1, 2, 1]
  );
  assert.equal(rows[0].color, m.COLORS[0]);
});

test("before the speakers are final, rows are unlabelled", () => {
  const rows = m.transcriptRows(record, false);
  assert.equal(rows.length, 3);
  assert.ok(rows.every((r) => r.speaker === null && r.color === null));
});

test("lanes: none before final; one per speaker after, as shares", () => {
  assert.deepEqual(m.lanes(record, 100, false), []);
  const lanes = m.lanes(record, 100, true);
  assert.deepEqual(
    lanes.map((l) => l.speaker),
    [1, 2]
  );
  assert.deepEqual(lanes[0].segments, [
    { left: 0, width: 5 },
    { left: 51, width: 8 },
  ]);
  assert.deepEqual(m.lanes(record, 0, true), []);
});

test("stages: done before, now at, todo after", () => {
  assert.deepEqual(
    m.stageItems(3).map((s) => s.status),
    ["done", "done", "now", "todo", "todo", "todo"]
  );
  assert.ok(m.stageItems(7).every((s) => s.status === "done"));
  assert.ok(m.stageItems(0).every((s) => s.status === "todo"));
});

test("speaker n always has the n-th colour", () => {
  assert.equal(m.speakerColor(0), m.COLORS[0]);
  assert.equal(m.speakerColor(12), m.COLORS[2]);
  assert.equal(m.speakerCount(record), 2);
});

test("only topics with a summary are listed", () => {
  assert.deepEqual(m.topicSummaries(record), [{ at: 0, text: "第一段" }]);
  assert.deepEqual(m.topicSummaries(null), []);
});

test("a failed poll keeps the last view and says the screen is stale", () => {
  const view = {
    now: 10,
    newest_received_at: 5,
    scene: "ready",
    meeting: null,
  };
  let model = m.poll(m.initial(), { ok: true, view }, 10);
  model = m.poll(model, { ok: false }, 12);
  assert.equal(model.view, view);
  assert.equal(model.stale, true);
  model = m.poll(model, { ok: true, view }, 14);
  assert.equal(model.stale, false);
});

test("a pin lets go when a newer live meeting arrives, not before", () => {
  const at = (newest) => ({
    now: 0,
    newest_received_at: newest,
    scene: "result",
    meeting: null,
  });
  let model = m.poll(m.initial(), { ok: true, view: at(100) }, 0);
  model = m.pinTo(model, "show");
  model = m.poll(model, { ok: true, view: at(100) }, 0);
  assert.equal(model.pin, "show");
  model = m.poll(model, { ok: true, view: at(200) }, 0);
  assert.equal(model.pin, null);
  let empty = m.poll(m.initial(), { ok: true, view: at(null) }, 0);
  empty = m.pinTo(empty, "show");
  empty = m.poll(empty, { ok: true, view: at(null) }, 0);
  assert.equal(empty.pin, "show");
  empty = m.poll(empty, { ok: true, view: at(1) }, 0);
  assert.equal(empty.pin, null);
  assert.equal(m.unpin(m.pinTo(model, "x")).pin, null);
});

test("the redraw key ignores the clock and the running time", () => {
  const a = {
    now: 1,
    scene: "processing",
    meeting: { meeting_id: "m", elapsed_s: 3, stage: 2 },
  };
  const b = {
    now: 9,
    scene: "processing",
    meeting: { meeting_id: "m", elapsed_s: 11, stage: 2 },
  };
  const c = {
    now: 9,
    scene: "processing",
    meeting: { meeting_id: "m", elapsed_s: 11, stage: 3 },
  };
  assert.equal(m.contentKey(a, null), m.contentKey(b, null));
  assert.notEqual(m.contentKey(b, null), m.contentKey(c, null));
  assert.notEqual(m.contentKey(b, null), m.contentKey(b, "show"));
});

test("the running time: to now while processing, to the end once ended", () => {
  assert.equal(m.elapsedNow({ started_at: 10, finished_at: null }, 25), 15);
  assert.equal(m.elapsedNow({ started_at: 10, finished_at: 40 }, 99), 30);
  assert.equal(m.elapsedNow({ started_at: null }, 99), null);
});

test("a meeting with no speech and a failed one say so, not done", () => {
  assert.equal(m.failureText({ state: "empty" }).title, "沒有偵測到語音");
  const failed = m.failureText({ state: "failed", error: "boom" });
  assert.equal(failed.title, "這段錄音沒有處理完成");
  assert.equal(failed.reason, "boom");
});

test("progress lines follow the stage", () => {
  const p = (stage, extra) =>
    m.progressText({
      stage,
      progress: { topics_done: 1, topics_total: 3 },
      turns: { done: 7, total: 20 },
      ahead: 0,
      ...extra,
    });
  assert.equal(p(0, { ahead: 2 }), "排隊中，前面還有 2 場");
  assert.equal(p(2), "分辨說話人：找出誰在什麼時候說話（第 2 / 3 段）");
  assert.equal(
    p(2, { progress: { topics_done: 0, topics_total: 1 } }),
    "分辨說話人：找出誰在什麼時候說話"
  );
  assert.equal(p(3), "轉文字：第 7 / 20 句（第 2 / 3 段）");
  assert.equal(p(4), "對齊說話人：比對整場會議的聲音特徵");
  assert.equal(p(5), "話題摘要：1 / 3");
  assert.equal(p(6), "正在寫會議結論……");
});

test("text is escaped before it is drawn", () => {
  assert.equal(
    m.escapeHtml(`<b a="1">&'`),
    "&lt;b a=&quot;1&quot;&gt;&amp;&#39;"
  );
  assert.equal(m.escapeHtml(null), "");
});

test("a poll that lets go of a pin asks again at once", () => {
  // The view it brought was asked for with the pin: drawing it would show
  // the showcase, still marked pinned, for one more poll.
  const at = (newest) => ({ now: 0, newest_received_at: newest });
  let model = m.poll(m.initial(), { ok: true, view: at(100) }, 0);
  assert.equal(model.again, false);
  model = m.pinTo(model, "show");
  model = m.poll(model, { ok: true, view: at(100) }, 0);
  assert.equal(model.again, false);
  model = m.poll(model, { ok: true, view: at(200) }, 0);
  assert.equal(model.pin, null);
  assert.equal(model.again, true);
  model = m.poll(model, { ok: true, view: at(200) }, 0);
  assert.equal(model.again, false);
});

test("Home holds the ready screen until a newer live meeting arrives", () => {
  const at = (newest) => ({
    now: 0,
    newest_received_at: newest,
    scene: "result",
    meeting: { meeting_id: "rehearsal" },
  });
  let model = m.poll(m.initial(), { ok: true, view: at(100) }, 0);
  assert.equal(m.shownScene(model), "result");
  model = m.holdReady(m.pinTo(model, "show"));
  assert.equal(model.pin, null);
  assert.equal(m.shownScene(model), "ready");
  model = m.poll(model, { ok: true, view: at(100) }, 0);
  assert.equal(m.shownScene(model), "ready");
  model = m.poll(model, { ok: true, view: at(200) }, 0);
  assert.equal(model.ready, false);
  assert.equal(model.again, false);
  assert.equal(m.shownScene(model), "result");
});

test("PageDown, PageUp and Esc end the hold", () => {
  const view = { now: 0, newest_received_at: 5, scene: "result" };
  const held = m.holdReady(m.poll(m.initial(), { ok: true, view }, 0));
  assert.equal(m.pinTo(held, "show").ready, false);
  assert.equal(m.unpin(held).ready, false);
  assert.equal(m.shownScene(m.initial()), null);
});

test("the redraw key changes when the ready screen is held", () => {
  const view = { now: 0, newest_received_at: 5, scene: "result" };
  assert.notEqual(m.contentKey(view, null, true), m.contentKey(view, null));
});

test("a record with no words has nothing for the transcript", () => {
  assert.equal(m.hasWords(record), true);
  assert.equal(
    m.hasWords({ topics: [{ error: "ASR failed", utterances: [] }] }),
    false
  );
  assert.equal(m.hasWords(null), false);
});

test("the stages are the board's own steps, in its order", () => {
  assert.deepEqual(m.STAGES, [
    "收到錄音",
    "分辨說話人",
    "轉文字",
    "對齊說話人",
    "話題摘要",
    "會議結論",
  ]);
  assert.equal(m.STATE_LABEL.linking, "對齊說話人");
});

test("diarizing has an estimated bar that stops short of the end", () => {
  // 240 s of audio: about 0.5 x 240 + 10 = 130 s expected.
  const meeting = { stage: 2, step_started_at: 100, topic_s: 240 };
  let bar = m.progressBar(meeting, 165);
  assert.equal(bar.estimate, true);
  assert.equal(bar.fraction, 0.5);
  assert.equal(m.leftText(bar), "預計還要約 1 分鐘");
  bar = m.progressBar(meeting, 1000);
  assert.equal(bar.fraction, 0.95);
  assert.equal(m.leftText(bar), "快好了");
});

test("recognition and summaries count what is done", () => {
  const recognising = { stage: 3, turns: { done: 3, total: 12 } };
  assert.deepEqual(m.progressBar(recognising, 0), {
    fraction: 0.25,
    estimate: false,
    left_s: null,
  });
  assert.equal(m.leftText(m.progressBar(recognising, 0)), "");
  const summaries = { stage: 5, progress: { topics_done: 1, topics_total: 4 } };
  assert.equal(m.progressBar(summaries, 0).fraction, 0.25);
});

test("the conclusion is estimated from when it began", () => {
  const bar = m.progressBar({ stage: 6, since: 100 }, 130);
  assert.equal(bar.left_s, 15);
  assert.equal(m.leftText(bar), "預計還要約 15 秒");
});

test("a stage with nothing to measure has no bar", () => {
  for (const stage of [0, 1, 4, 7]) {
    assert.equal(m.progressBar({ stage }, 0), null);
  }
  assert.equal(m.progressBar({ stage: 2 }, 0), null);
  assert.equal(m.progressBar(null, 0), null);
});

test("a meeting in the list says its length, speakers and state", () => {
  const row = m.listRow({
    meeting_id: "pc-1",
    title: "週會",
    state: "archived",
    received_at: null,
    duration_s: 5176,
    speakers: 9,
    showcase: false,
  });
  assert.equal(row.title, "週會");
  assert.equal(row.detail, "1:26:16 · 9 人 · 完成");
  const untitled = m.listRow({
    meeting_id: "pc-2",
    title: "",
    state: "transcribing",
    duration_s: 60,
  });
  assert.equal(untitled.title, "pc-2");
  assert.equal(untitled.detail, "1:00 · 分辨說話人＋轉文字");
});
