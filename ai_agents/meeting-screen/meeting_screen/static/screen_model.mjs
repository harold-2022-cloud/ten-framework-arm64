//
// This file is part of TEN Framework, an open source project.
// Licensed under the Apache License, Version 2.0.
//
// The demo screen's model: what the page draws, worked out from /api/view.
// No DOM here, so node --test runs it (tests/screen_model.test.mjs).

// The board's own steps, in its order: each topic's speakers are told apart
// (pyannote, 3D-Speaker), then each turn is recognised (SenseVoice); linking
// then gives each person one number across the meeting.
export const STAGES = [
  "收到錄音",
  "分辨說話人",
  "轉文字",
  "對齊說話人",
  "話題摘要",
  "會議結論",
];

// How long the steps with no progress of their own take on the board: a
// topic's speakers are told apart in 0.71-0.93 of its length for topics of
// 5-10 minutes (26 topics, 2026-10-16/17; the 0.5 first used came from a
// short clip) plus loading two models; the conclusion took 41 s for a
// 12-minute meeting and about 80 for 38 minutes. Rough on purpose: the
// bar they drive says 預計 and stops short of the end until the step ends;
// set a little high, it ends early rather than sitting at 快好了.
export const DIARIZE_PER_AUDIO_S = 0.85;
export const DIARIZE_START_S = 10;
export const CONCLUDE_S = 45;
const ESTIMATE_CAP = 0.95;

// Ten fixed colours: speaker n is always the n-th, on the dark background.
export const COLORS = [
  "#4cc2ff",
  "#ffb84c",
  "#7ee081",
  "#ff7aa8",
  "#b79cff",
  "#5ce1d6",
  "#ffe066",
  "#ff8f66",
  "#9ad1ff",
  "#d6a2e8",
];

export const STATE_LABEL = {
  queued: "排隊中",
  received: "收到錄音",
  decoding: "收到錄音",
  transcribing: "分辨說話人＋轉文字",
  linking: "對齊說話人",
  summarising: "話題摘要",
  concluding: "會議結論",
  archived: "完成",
  failed: "未完成",
  empty: "沒有語音",
};

const pad = (n) => String(n).padStart(2, "0");

export function mmss(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(s / 3600);
  const min = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}:${pad(min)}:${pad(s % 60)}` : `${min}:${pad(s % 60)}`;
}

export function minutesText(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  return s < 60 ? `${s} 秒` : `${Math.round(s / 60)} 分鐘`;
}

const ENTITIES = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

export function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ENTITIES[c]);
}

export function speakerColor(n) {
  const i = Number(n) || 0;
  return COLORS[((i % COLORS.length) + COLORS.length) % COLORS.length];
}

export function stageItems(stage) {
  return STAGES.map((name, i) => {
    const n = i + 1;
    let status = "todo";
    if (stage > n) {
      status = "done";
    } else if (stage === n) {
      status = "now";
    }
    return { name, status };
  });
}

function topicsOf(record) {
  return record && Array.isArray(record.topics) ? record.topics : [];
}

function utterancesOf(topic) {
  return Array.isArray(topic.utterances) ? topic.utterances : [];
}

// Every utterance in meeting order, at its time in the recording. A speaker
// number is shown -- from 1, in its colour -- only once the speakers are
// final: before linking, each topic numbers its own.
export function transcriptRows(record, speakersFinal) {
  const rows = [];
  for (const topic of topicsOf(record)) {
    const base = Number(topic.start_s) || 0;
    for (const u of utterancesOf(topic)) {
      const known = speakersFinal && Number.isInteger(u.speaker);
      rows.push({
        at: base + (Number(u.start_s) || 0),
        text: String(u.text ?? ""),
        speaker: known ? u.speaker + 1 : null,
        color: known ? speakerColor(u.speaker) : null,
      });
    }
  }
  rows.sort((a, b) => a.at - b.at);
  return rows;
}

const round2 = (x) => Math.round(x * 100) / 100;

// Who spoke when: one lane per speaker in number order, each turn a share
// of the recording; none before the speakers are final.
export function lanes(record, duration, speakersFinal) {
  const whole = Number(duration) || 0;
  if (!speakersFinal || whole <= 0) {
    return [];
  }
  const bySpeaker = new Map();
  for (const topic of topicsOf(record)) {
    const base = Number(topic.start_s) || 0;
    for (const u of utterancesOf(topic)) {
      if (!Number.isInteger(u.speaker)) {
        continue;
      }
      const start = base + (Number(u.start_s) || 0);
      const end = base + (Number(u.end_s) || 0);
      const left = Math.min(100, Math.max(0, (start / whole) * 100));
      const share = Math.max(0.2, ((end - start) / whole) * 100);
      const width = Math.min(100 - left, share);
      if (!bySpeaker.has(u.speaker)) {
        bySpeaker.set(u.speaker, []);
      }
      bySpeaker
        .get(u.speaker)
        .push({ left: round2(left), width: round2(width) });
    }
  }
  return [...bySpeaker.keys()]
    .sort((a, b) => a - b)
    .map((n) => ({
      speaker: n + 1,
      color: speakerColor(n),
      segments: bySpeaker.get(n),
    }));
}

// Whether any topic has an utterance: a meeting whose every topic failed
// has none, and no transcript to show.
export function hasWords(record) {
  return topicsOf(record).some((t) => utterancesOf(t).length > 0);
}

// A line of the meeting list: its title, else its id; then its length, the
// speakers it found and its state.
export function listRow(item) {
  const parts = [];
  if (Number(item.duration_s) > 0) {
    parts.push(mmss(item.duration_s));
  }
  if (Number.isInteger(item.speakers)) {
    parts.push(`${item.speakers} 人`);
  }
  parts.push(STATE_LABEL[item.state] || item.state || "");
  return {
    title: item.title || item.meeting_id,
    detail: parts.filter(Boolean).join(" · "),
  };
}

// The conclusion's lines as the LLM writes them: "一、會議結論" headings,
// "1. ..." items, and paragraphs (minutes_html.py reads them the same way).
const HEADING = /^[一二三四五六七八九十]+、\s*(.+)$/;
// "1. ...", not "1.5 hours" or "2026.10.20": one or two digits, no digit after.
const ITEM = /^\d{1,2}[.、．](?!\d)\s*(.+)$/;

export function summaryBlocks(text) {
  const blocks = [];
  for (const raw of String(text ?? "").split("\n")) {
    const line = raw.trim();
    if (!line) {
      continue;
    }
    const heading = HEADING.exec(line);
    const item = ITEM.exec(line);
    if (heading) {
      blocks.push({ kind: "heading", text: heading[1] });
    } else if (item) {
      blocks.push({ kind: "item", text: item[1] });
    } else {
      blocks.push({ kind: "para", text: line });
    }
  }
  return blocks;
}

export function speakerCount(record) {
  const seen = new Set();
  for (const topic of topicsOf(record)) {
    for (const u of utterancesOf(topic)) {
      if (Number.isInteger(u.speaker)) {
        seen.add(u.speaker);
      }
    }
  }
  return seen.size;
}

export function topicSummaries(record) {
  return topicsOf(record)
    .filter((t) => t.summary)
    .map((t) => ({ at: Number(t.start_s) || 0, text: String(t.summary) }));
}

function topicOf(meeting) {
  const progress = meeting.progress || {};
  const done = Number(progress.topics_done) || 0;
  const total = Number(progress.topics_total) || 0;
  return { done, total, at: total > 0 ? Math.min(done + 1, total) : 0 };
}

// The line under the stages while a meeting is processed.
export function progressText(meeting) {
  const topic = topicOf(meeting);
  const turns = meeting.turns || {};
  const ofTopics =
    topic.total > 1 ? `（第 ${topic.at} / ${topic.total} 段）` : "";
  switch (meeting.stage) {
    case 0:
      return meeting.ahead > 0
        ? `排隊中，前面還有 ${meeting.ahead} 場`
        : "排隊中，馬上開始";
    case 1:
      return "收到錄音，準備處理";
    case 2:
      return `分辨說話人：找出誰在什麼時候說話${ofTopics}`;
    case 3:
      return Number(turns.total) > 0
        ? `轉文字：第 ${turns.done} / ${turns.total} 句${ofTopics}`
        : `轉文字${ofTopics}`;
    case 4:
      return "對齊說話人：比對整場會議的聲音特徵";
    case 5:
      return topic.total > 0
        ? `話題摘要：${topic.done} / ${topic.total}`
        : "話題摘要";
    case 6:
      return "正在寫會議結論……";
    default:
      return "";
  }
}

function estimated(startedAt, expected, now) {
  const spent = Math.max(0, now - startedAt);
  return {
    fraction: Math.min(ESTIMATE_CAP, spent / expected),
    estimate: true,
    left_s: Math.max(0, expected - spent),
  };
}

// The bar under the line: the share done, and whether it is an estimate
// (with the seconds it expects are left). null when the stage has nothing
// to measure. Redrawn once a second, never animated.
export function progressBar(meeting, serverNow) {
  if (!meeting) {
    return null;
  }
  const turns = meeting.turns || {};
  const topic = topicOf(meeting);
  if (
    meeting.stage === 2 &&
    meeting.step_started_at != null &&
    meeting.topic_s != null
  ) {
    const expected = meeting.topic_s * DIARIZE_PER_AUDIO_S + DIARIZE_START_S;
    return estimated(meeting.step_started_at, expected, serverNow);
  }
  if (meeting.stage === 3 && Number(turns.total) > 0) {
    const fraction = Math.min(1, Number(turns.done) / Number(turns.total));
    return { fraction, estimate: false, left_s: null };
  }
  if (meeting.stage === 5 && topic.total > 0) {
    return {
      fraction: topic.done / topic.total,
      estimate: false,
      left_s: null,
    };
  }
  if (meeting.stage === 6 && meeting.since != null) {
    return estimated(meeting.since, CONCLUDE_S, serverNow);
  }
  return null;
}

export function leftText(bar) {
  if (!bar || !bar.estimate) {
    return "";
  }
  if (bar.left_s < 5) {
    return "快好了";
  }
  return bar.left_s < 60
    ? `預計還要約 ${Math.ceil(bar.left_s / 5) * 5} 秒`
    : `預計還要約 ${Math.round(bar.left_s / 60)} 分鐘`;
}

// What a meeting that did not finish says: never anything like done.
export function failureText(meeting) {
  if (meeting.state === "empty") {
    return { title: "沒有偵測到語音", reason: "" };
  }
  return { title: "這段錄音沒有處理完成", reason: String(meeting.error ?? "") };
}

// The page's own state: the last view; a hold -- a pinned meeting, or the
// ready screen held by Home -- and the newest live meeting when it began;
// whether the service stopped answering; the server clock's offset from
// this one; and whether to ask again at once.
export function initial() {
  return {
    view: null,
    pin: null,
    ready: false,
    since: null,
    stale: false,
    offset: 0,
    again: false,
  };
}

// A poll folded into the state. A failed one keeps the last view, marked
// stale. A hold lets go once a live meeting newer than any at its start
// arrives: a live upload always wins. A pin let go asks again at once: the
// view this poll brought was asked for with the pin.
export function poll(model, outcome, clientNow) {
  if (!outcome.ok) {
    return { ...model, stale: true, again: false };
  }
  const view = outcome.view;
  const newest = view.newest_received_at ?? null;
  const held = model.pin !== null || model.ready;
  const arrived =
    held && newest !== null && (model.since === null || newest > model.since);
  const serverNow = Number(view.now);
  return {
    view,
    pin: arrived ? null : model.pin,
    ready: arrived ? false : model.ready,
    since: arrived ? null : model.since,
    stale: false,
    offset: Number.isFinite(serverNow) ? serverNow - clientNow : 0,
    again: arrived && model.pin !== null,
  };
}

function newestOf(model) {
  return model.view ? (model.view.newest_received_at ?? null) : null;
}

export function pinTo(model, meetingId) {
  return { ...model, pin: meetingId, ready: false, since: newestOf(model) };
}

// Home: the ready screen, until someone uploads -- for the opening, after
// a rehearsal that would otherwise stay on screen for recent_min.
export function holdReady(model) {
  return { ...model, pin: null, ready: true, since: newestOf(model) };
}

export function unpin(model) {
  return { ...model, pin: null, ready: false, since: null };
}

export function shownScene(model) {
  if (model.ready) {
    return "ready";
  }
  return model.view ? model.view.scene : null;
}

// What decides a redraw: the view without the clock and the running time,
// which tick by themselves every second.
export function contentKey(view, pin, ready = false) {
  if (!view) {
    return "";
  }
  const meeting = view.meeting ? { ...view.meeting, elapsed_s: null } : null;
  return JSON.stringify({ ...view, now: null, meeting, pin, ready });
}

export function elapsedNow(meeting, serverNow) {
  if (!meeting || meeting.started_at == null) {
    return null;
  }
  const end = meeting.finished_at ?? serverNow;
  return Math.max(0, end - meeting.started_at);
}
