//
// This file is part of TEN Framework, an open source project.
// Licensed under the Apache License, Version 2.0.
//
// The demo screen in the browser: draws what /api/view says. It asks every
// 2 s and redraws only when the content changed -- no animation, the
// board's browser manages 24 frames a second -- while the clock and the
// running time tick by themselves. The rules live in screen_model.mjs.
import {
  contentKey,
  elapsedNow,
  escapeHtml,
  failureText,
  initial,
  lanes,
  minutesText,
  mmss,
  pinTo,
  poll,
  progressText,
  STATE_LABEL,
  speakerCount,
  stageItems,
  topicSummaries,
  transcriptRows,
  unpin,
} from "/static/screen_model.mjs";

const POLL_MS = 2000;
const byId = (id) => document.getElementById(id);
let model = initial();
let drawn = null;
let timer = null;
const list = { open: false, items: [], index: 0 };

const serverNow = () => Date.now() / 1000 + model.offset;

function stagesHtml(stage) {
  const items = stageItems(stage)
    .map((s) => `<li class="${s.status}">${s.name}</li>`)
    .join("");
  return `<ol class="stages">${items}</ol>`;
}

function rowsHtml(meeting) {
  const rows = transcriptRows(meeting.record, meeting.speakers_final);
  if (rows.length === 0) {
    return '<p class="muted">逐字稿會在這裡出現</p>';
  }
  return rows
    .map((r) => {
      const who =
        r.speaker === null
          ? ""
          : `<span class="who" style="color:${r.color}">說話人${r.speaker}</span>`;
      const grey = r.speaker === null ? " grey" : "";
      return `<div class="row${grey}"><span class="at">${mmss(r.at)}</span>${who}<span class="text">${escapeHtml(r.text)}</span></div>`;
    })
    .join("");
}

function lanesHtml(meeting) {
  const rows = lanes(
    meeting.record,
    meeting.duration_s,
    meeting.speakers_final
  );
  if (rows.length === 0) {
    return "";
  }
  const html = rows
    .map((lane) => {
      const bars = lane.segments
        .map(
          (s) =>
            `<i style="left:${s.left}%;width:${s.width}%;background:${lane.color}"></i>`
        )
        .join("");
      return `<div class="lane"><span class="who" style="color:${lane.color}">說話人${lane.speaker}</span><div class="track">${bars}</div></div>`;
    })
    .join("");
  return `<div class="lanes">${html}</div>`;
}

function topicsHtml(record) {
  return topicSummaries(record)
    .map(
      (t) => `<p><span class="at">${mmss(t.at)}</span>${escapeHtml(t.text)}</p>`
    )
    .join("");
}

function titleLine(meeting, label) {
  const tag = meeting.showcase ? '<span class="tag">範例會議</span>' : "";
  const title = escapeHtml(meeting.title || "未命名的會議");
  return `<div class="meta"><span>${tag}${title} · 錄音 ${mmss(meeting.duration_s)}</span><span>${label} <b id="elapsed"></b></span></div>`;
}

function pinnedNote(view) {
  return view.pinned
    ? '<p class="hint">已固定這一場 · PageUp 回到現場</p>'
    : "";
}

function showcaseHint(view) {
  return view.showcase_id ? '<p class="hint">按 PageDown 看完整範例</p>' : "";
}

function transcriptHtml(meeting) {
  return `<div class="transcript" id="transcript">${rowsHtml(meeting)}</div>`;
}

const SCENES = {
  ready(view) {
    return `<section class="ready">
<h1>會議紀錄，全程在這台裝置上完成</h1>
<p class="lead">用手機 App 錄下會議，上傳到這台裝置</p>
<div class="cards">
<div class="card"><b>本機</b><h2>分辨說話人</h2><p>pyannote 分段 + 3D-Speaker 聲音特徵</p></div>
<div class="card"><b>本機</b><h2>語音轉文字</h2><p>SenseVoice</p></div>
<div class="card"><b>本機</b><h2>會議摘要</h2><p>Qwen3.5-4B</p></div>
</div>${showcaseHint(view)}</section>`;
  },
  queued(view) {
    const meeting = view.meeting;
    return `<section class="processing">${stagesHtml(0)}${titleLine(meeting, "已用時")}<p class="big">${progressText(meeting)}</p>${pinnedNote(view)}</section>`;
  },
  processing(view) {
    const meeting = view.meeting;
    const found = meeting.speakers_final
      ? `<p class="big">分辨出 ${speakerCount(meeting.record)} 位說話人</p>`
      : "";
    return `<section class="processing">${stagesHtml(meeting.stage)}${titleLine(meeting, "已用時")}<p class="status">${progressText(meeting)}</p>${found}${lanesHtml(meeting)}<div class="topics">${topicsHtml(meeting.record)}</div>${transcriptHtml(meeting)}${pinnedNote(view)}</section>`;
  },
  result(view) {
    const meeting = view.meeting;
    const record = meeting.record || {};
    const topics = Array.isArray(record.topics) ? record.topics.length : 0;
    return `<section class="result">
<div class="numbers">
<div>會議長度<b>${minutesText(meeting.duration_s)}</b></div>
<div>說話人<b>${speakerCount(record)} 位</b></div>
<div>話題<b>${topics} 個</b></div>
<div>處理用時<b id="elapsed"></b></div>
</div>${lanesHtml(meeting)}
<div class="columns">
<div class="left"><h2>${escapeHtml(meeting.title || "會議摘要")}</h2><div class="summary">${escapeHtml(record.summary || "")}</div><div class="topics">${topicsHtml(record)}</div></div>
${transcriptHtml(meeting)}
</div>${pinnedNote(view)}</section>`;
  },
  failed(view) {
    const meeting = view.meeting;
    const text = failureText(meeting);
    const reason = text.reason
      ? `<p class="error">${escapeHtml(text.reason)}</p>`
      : "";
    return `<section class="failed">${titleLine(meeting, "用時")}<h1>${text.title}</h1>${reason}${showcaseHint(view)}${transcriptHtml(meeting)}${pinnedNote(view)}</section>`;
  },
};

function tick() {
  const now = new Date();
  byId("clock").textContent =
    `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
  const slot = byId("elapsed");
  const meeting = model.view ? model.view.meeting : null;
  if (!slot || !meeting) {
    return;
  }
  const seconds = elapsedNow(meeting, serverNow());
  if (seconds === null) {
    slot.textContent = "—";
  } else {
    slot.textContent =
      model.view.scene === "result" ? minutesText(seconds) : mmss(seconds);
  }
}

function draw() {
  byId("stale").hidden = !model.stale;
  const view = model.view;
  if (!view) {
    return;
  }
  const key = contentKey(view, model.pin);
  if (key === drawn) {
    return;
  }
  const before = byId("transcript");
  const same =
    before &&
    view.meeting &&
    before.dataset.meeting === view.meeting.meeting_id;
  const keep = same ? before.scrollTop : 0;
  drawn = key;
  const scene = SCENES[view.scene];
  byId("scene").innerHTML = scene ? scene(view) : "";
  const transcript = byId("transcript");
  if (transcript && view.meeting) {
    transcript.dataset.meeting = view.meeting.meeting_id;
    // While it is processed the newest words matter; a result stays where
    // it was read to.
    transcript.scrollTop =
      view.scene === "processing" ? transcript.scrollHeight : keep;
  }
  tick();
}

async function refresh() {
  clearTimeout(timer);
  const url = model.pin
    ? `/api/view?pin=${encodeURIComponent(model.pin)}`
    : "/api/view";
  try {
    const answer = await fetch(url, { cache: "no-store" });
    if (answer.status === 404 && model.pin) {
      model = unpin(model);
    } else if (answer.ok) {
      model = poll(
        model,
        { ok: true, view: await answer.json() },
        Date.now() / 1000
      );
    } else {
      model = poll(model, { ok: false }, Date.now() / 1000);
    }
  } catch {
    model = poll(model, { ok: false }, Date.now() / 1000);
  }
  draw();
  timer = setTimeout(refresh, POLL_MS);
}

function drawList() {
  const box = byId("list");
  box.hidden = false;
  if (list.items.length === 0) {
    box.innerHTML = '<p class="muted">還沒有會議</p>';
    return;
  }
  box.innerHTML = list.items
    .map((item, i) => {
      const when = item.received_at
        ? new Date(item.received_at * 1000).toLocaleString("zh-TW", {
            hour12: false,
          })
        : "";
      const tag = item.showcase ? '<span class="tag">範例</span>' : "";
      const on = i === list.index ? " on" : "";
      return `<div class="item${on}" data-id="${escapeHtml(item.meeting_id)}">${tag}${escapeHtml(item.title || item.meeting_id)}<span class="muted"> · ${STATE_LABEL[item.state] || ""} · ${when}</span></div>`;
    })
    .join("");
  box.querySelector(".item.on")?.scrollIntoView({ block: "nearest" });
}

async function openList() {
  try {
    const answer = await fetch("/api/meetings", { cache: "no-store" });
    list.items = answer.ok ? await answer.json() : [];
  } catch {
    list.items = [];
  }
  list.open = true;
  list.index = 0;
  drawList();
}

function closeList() {
  list.open = false;
  byId("list").hidden = true;
}

function pinMeeting(meetingId) {
  closeList();
  model = pinTo(model, meetingId);
  refresh();
}

function onListKey(event) {
  const last = list.items.length - 1;
  if (event.key === "ArrowDown") {
    list.index = Math.min(last, list.index + 1);
    drawList();
  } else if (event.key === "ArrowUp") {
    list.index = Math.max(0, list.index - 1);
    drawList();
  } else if (event.key === "Enter" && list.items[list.index]) {
    pinMeeting(list.items[list.index].meeting_id);
  } else if (event.key === "Escape" || event.key === "l" || event.key === "L") {
    closeList();
  } else {
    return;
  }
  event.preventDefault();
}

function onKey(event) {
  if (list.open) {
    onListKey(event);
    return;
  }
  const transcript = byId("transcript");
  switch (event.key) {
    case "PageDown":
    case "ArrowRight":
      if (model.view?.showcase_id) {
        model = pinTo(model, model.view.showcase_id);
        refresh();
      }
      break;
    case "PageUp":
    case "ArrowLeft":
    case "Escape":
      model = unpin(model);
      refresh();
      break;
    case "ArrowDown":
      if (transcript) {
        transcript.scrollTop += 120;
      }
      break;
    case "ArrowUp":
      if (transcript) {
        transcript.scrollTop -= 120;
      }
      break;
    case "l":
    case "L":
      openList();
      break;
    default:
      return;
  }
  event.preventDefault();
}

document.addEventListener("keydown", onKey);
byId("list").addEventListener("click", (event) => {
  const item = event.target.closest(".item");
  if (item) {
    pinMeeting(item.dataset.id);
  }
});
setInterval(tick, 1000);
refresh();
