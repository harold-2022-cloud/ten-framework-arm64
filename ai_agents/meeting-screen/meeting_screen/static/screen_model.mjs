//
// This file is part of TEN Framework, an open source project.
// Licensed under the Apache License, Version 2.0.
//
// The demo screen's model: what the page draws, worked out from /api/view.
// No DOM here, so node --test runs it (tests/screen_model.test.mjs).

export const STAGES = [
  "收到錄音",
  "語音轉文字",
  "分辨說話人",
  "話題摘要",
  "會議結論",
];

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
  transcribing: "語音轉文字",
  linking: "分辨說話人",
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

// The line under the stages while a meeting is processed.
export function progressText(meeting) {
  const progress = meeting.progress || {};
  const done = Number(progress.topics_done) || 0;
  const total = Number(progress.topics_total) || 0;
  switch (meeting.stage) {
    case 0:
      return meeting.ahead > 0
        ? `排隊中，前面還有 ${meeting.ahead} 場`
        : "排隊中，馬上開始";
    case 1:
      return "收到錄音，準備處理";
    case 2:
      return total > 0
        ? `語音轉文字：第 ${Math.min(done + 1, total)} / ${total} 段`
        : "語音轉文字";
    case 3:
      return "分辨說話人：比對整場會議的聲音特徵";
    case 4:
      return total > 0 ? `話題摘要：${done} / ${total}` : "話題摘要";
    case 5:
      return "正在寫會議結論……";
    default:
      return "";
  }
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
