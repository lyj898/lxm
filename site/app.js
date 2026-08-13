// H2 Maths question bank - reads the committed SQLite file with sql.js and
// renders question cards. No build step: plain ES modules + CDN libraries.

const SQL_WASM_BASE = "https://cdnjs.cloudflare.com/ajax/libs/sql.js/1.10.3/";
// jsdelivr rather than cdnjs: cdnjs serves the pdf.js bundle but 403s on the
// cmaps/ and standard_fonts/ directories, which these papers need for symbol
// and CID fonts.
const PDFJS_BASE = "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.6.82/";

// Deployed, the site sits at the domain root with data/ beside it. Served
// locally from the repo root, index.html is under site/ and data/ is one level
// up - so try both.
const DB_CANDIDATES = ["data/bank.sqlite", "../data/bank.sqlite"];

const QUERY = `
  SELECT q.id, q.q_number, q.part_labels, q.page_start, q.page_end,
         q.marks_total, q.full_text, q.needs_ocr, q.extract_confidence,
         p.school, p.school_code, p.year, p.paper_no, p.exam_type,
         p.rel_path AS qp_path,
         (SELECT GROUP_CONCAT(qt.topic_code || ':' || qt.method, '|')
            FROM question_topics qt WHERE qt.question_id = q.id) AS tag_blob,
         (SELECT ms.rel_path FROM papers ms
            WHERE ms.school_code = p.school_code AND ms.year = p.year
              AND ms.exam_type = p.exam_type
              AND (ms.paper_no IS p.paper_no)
              AND ms.doc_type IN ('ms', 'combined')
            ORDER BY ms.doc_type LIMIT 1) AS ms_path
  FROM questions q
  JOIN papers p ON p.id = q.paper_id
  ORDER BY p.year DESC, p.school_code, p.paper_no, q.q_number
`;

const els = {
  results: document.getElementById("results"),
  empty: document.getElementById("empty"),
  count: document.getElementById("count"),
  text: document.getElementById("f-text"),
  topic: document.getElementById("f-topic"),
  school: document.getElementById("f-school"),
  year: document.getElementById("f-year"),
  marksMin: document.getElementById("f-marks-min"),
  marksMax: document.getElementById("f-marks-max"),
  hasMs: document.getElementById("f-has-ms"),
  reset: document.getElementById("f-reset"),
  viewer: document.getElementById("viewer"),
  viewerTitle: document.getElementById("viewer-title"),
  viewerError: document.getElementById("viewer-error"),
  canvas: document.getElementById("pdf-canvas"),
  pdfPrev: document.getElementById("pdf-prev"),
  pdfNext: document.getElementById("pdf-next"),
  pdfPage: document.getElementById("pdf-page"),
  pdfRaw: document.getElementById("pdf-raw"),
  viewerClose: document.getElementById("viewer-close"),
};

let questions = [];
let topicNames = new Map();
let dataRoot = "";

// ---------------------------------------------------------------- data load

async function loadDb() {
  const SQL = await initSqlJs({ locateFile: (f) => SQL_WASM_BASE + f });
  let bytes = null;
  let lastError = null;
  for (const path of DB_CANDIDATES) {
    try {
      // Revalidate rather than trusting the cache: the pipeline rewrites this
      // file, so a cached copy silently shows a stale bank (e.g. no topics).
      const resp = await fetch(path, { cache: "no-cache" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      bytes = new Uint8Array(await resp.arrayBuffer());
      dataRoot = path.slice(0, path.indexOf("data/"));
      break;
    } catch (err) {
      lastError = err;
    }
  }
  if (!bytes) throw new Error(`could not load the database (${lastError})`);
  return new SQL.Database(bytes);
}

function rowsOf(db, sql) {
  const out = [];
  const stmt = db.prepare(sql);
  while (stmt.step()) out.push(stmt.getAsObject());
  stmt.free();
  return out;
}

function parseTags(blob) {
  if (!blob) return [];
  return blob.split("|").map((piece) => {
    const [code, method] = piece.split(":");
    return { code, method: method || "rule" };
  });
}

// ---------------------------------------------------------------- rendering

function optionsFor(select, values, label) {
  select.length = 1;
  select.options[0].textContent = label;
  for (const v of values) {
    const opt = document.createElement("option");
    opt.value = String(v.value);
    opt.textContent = v.text;
    select.appendChild(opt);
  }
}

function paperRef(q) {
  const pno = q.paper_no ? `P${q.paper_no}` : "P?";
  return `${q.school_code} ${q.year} ${pno} Q${q.q_number}`;
}

function card(q) {
  const el = document.createElement("article");
  el.className = "card";

  const head = document.createElement("div");
  head.className = "card-head";

  const ref = document.createElement("span");
  ref.className = "qref";
  ref.textContent = paperRef(q);
  head.appendChild(ref);

  const bits = [];
  if (q.marks_total) bits.push(`${q.marks_total} marks`);
  const parts = JSON.parse(q.part_labels || "[]");
  if (parts.length) {
    bits.push(`${parts.length} part${parts.length === 1 ? "" : "s"} ${parts.join("")}`);
  }
  if (q.page_start) {
    bits.push(q.page_end && q.page_end !== q.page_start
      ? `pp. ${q.page_start}–${q.page_end}`
      : `p. ${q.page_start}`);
  }
  bits.push(q.school);
  const meta = document.createElement("span");
  meta.className = "meta";
  meta.textContent = bits.join(" · ");
  head.appendChild(meta);
  el.appendChild(head);

  const text = document.createElement("p");
  text.className = "qtext";
  text.textContent = q.full_text;
  el.appendChild(text);

  const tags = document.createElement("div");
  tags.className = "tags";
  for (const t of q.tags) {
    const span = document.createElement("span");
    span.className = `tag ${t.method}`;
    span.title = `${topicNames.get(t.code) || t.code} (tagged by ${t.method})`;
    span.textContent = topicNames.get(t.code) || t.code;
    tags.appendChild(span);
  }
  if (q.needs_ocr) {
    const flag = document.createElement("span");
    flag.className = "flag";
    flag.textContent = "needs OCR";
    tags.appendChild(flag);
  }
  if (tags.children.length) el.appendChild(tags);

  const actions = document.createElement("div");
  actions.className = "card-actions";

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.textContent = "Show full text";
  toggle.addEventListener("click", () => {
    const open = text.classList.toggle("open");
    toggle.textContent = open ? "Collapse text" : "Show full text";
  });
  actions.appendChild(toggle);

  const openQp = document.createElement("button");
  openQp.type = "button";
  openQp.className = "primary";
  openQp.textContent = `Question paper · p.${q.page_start || 1}`;
  openQp.addEventListener("click", () =>
    openViewer(q.qp_path, q.page_start || 1, `${paperRef(q)} — question paper`));
  actions.appendChild(openQp);

  if (q.ms_path) {
    const openMs = document.createElement("button");
    openMs.type = "button";
    openMs.textContent = "Solutions";
    openMs.addEventListener("click", () =>
      openViewer(q.ms_path, 1, `${paperRef(q)} — solutions`));
    actions.appendChild(openMs);
  }

  el.appendChild(actions);
  return el;
}

function visible() {
  const needle = els.text.value.trim().toLowerCase();
  const topic = els.topic.value;
  const school = els.school.value;
  const year = els.year.value;
  const min = els.marksMin.value === "" ? null : Number(els.marksMin.value);
  const max = els.marksMax.value === "" ? null : Number(els.marksMax.value);
  const needMs = els.hasMs.checked;

  return questions.filter((q) => {
    if (needle && !q.full_text.toLowerCase().includes(needle)) return false;
    if (topic && !q.tags.some((t) => t.code === topic)) return false;
    if (school && q.school_code !== school) return false;
    if (year && String(q.year) !== year) return false;
    if (needMs && !q.ms_path) return false;
    if (min !== null && (q.marks_total ?? -1) < min) return false;
    if (max !== null && (q.marks_total ?? 999) > max) return false;
    return true;
  });
}

function render() {
  const shown = visible();
  els.results.replaceChildren(...shown.map(card));
  els.empty.hidden = shown.length > 0;
  const marks = shown.reduce((a, q) => a + (q.marks_total || 0), 0);
  els.count.textContent =
    `${shown.length} of ${questions.length} questions · ${marks} marks`;
}

// ---------------------------------------------------------------- pdf viewer

let pdfDoc = null;
let pdfPageNo = 1;
let pdfjsLib = null;

async function getPdfjs() {
  if (!pdfjsLib) {
    pdfjsLib = await import(`${PDFJS_BASE}build/pdf.min.mjs`);
    pdfjsLib.GlobalWorkerOptions.workerSrc = `${PDFJS_BASE}build/pdf.worker.min.mjs`;
  }
  return pdfjsLib;
}

async function openViewer(relPath, page, title) {
  const url = dataRoot + relPath;
  els.viewerTitle.textContent = title;
  els.pdfRaw.href = `${url}#page=${page}`;
  els.viewerError.hidden = true;
  if (!els.viewer.open) els.viewer.showModal();

  try {
    const lib = await getPdfjs();
    pdfDoc = await lib.getDocument({
      url,
      cMapUrl: `${PDFJS_BASE}cmaps/`,
      cMapPacked: true,
      standardFontDataUrl: `${PDFJS_BASE}standard_fonts/`,
    }).promise;
    pdfPageNo = Math.min(Math.max(1, page), pdfDoc.numPages);
    await drawPage();
  } catch (err) {
    els.viewerError.hidden = false;
    els.viewerError.textContent =
      `Could not render this PDF here (${err}). Use "Open PDF" instead.`;
  }
}

async function drawPage() {
  if (!pdfDoc) return;
  const page = await pdfDoc.getPage(pdfPageNo);
  const wrapWidth = els.canvas.parentElement.clientWidth - 24;
  const base = page.getViewport({ scale: 1 });
  const scale = Math.min(2, Math.max(0.5, wrapWidth / base.width));
  const viewport = page.getViewport({ scale: scale * (window.devicePixelRatio || 1) });

  els.canvas.width = viewport.width;
  els.canvas.height = viewport.height;
  els.canvas.style.width = `${viewport.width / (window.devicePixelRatio || 1)}px`;
  await page.render({ canvasContext: els.canvas.getContext("2d"), viewport }).promise;
  els.pdfPage.textContent = `${pdfPageNo} / ${pdfDoc.numPages}`;
}

els.pdfPrev.addEventListener("click", async () => {
  if (pdfDoc && pdfPageNo > 1) { pdfPageNo--; await drawPage(); }
});
els.pdfNext.addEventListener("click", async () => {
  if (pdfDoc && pdfPageNo < pdfDoc.numPages) { pdfPageNo++; await drawPage(); }
});
els.viewerClose.addEventListener("click", () => els.viewer.close());
els.viewer.addEventListener("close", () => { pdfDoc = null; });

// ---------------------------------------------------------------- boot

for (const el of [els.text, els.marksMin, els.marksMax]) {
  el.addEventListener("input", render);
}
for (const el of [els.topic, els.school, els.year, els.hasMs]) {
  el.addEventListener("change", render);
}
els.reset.addEventListener("click", () => {
  els.text.value = "";
  els.topic.value = "";
  els.school.value = "";
  els.year.value = "";
  els.marksMin.value = "";
  els.marksMax.value = "";
  els.hasMs.checked = false;
  render();
});

(async function boot() {
  try {
    const db = await loadDb();

    for (const t of rowsOf(db, "SELECT code, name, strand FROM topics ORDER BY strand, name")) {
      topicNames.set(t.code, t.name);
    }

    questions = rowsOf(db, QUERY).map((q) => ({ ...q, tags: parseTags(q.tag_blob) }));

    const usedTopics = new Set(questions.flatMap((q) => q.tags.map((t) => t.code)));
    optionsFor(
      els.topic,
      [...topicNames.entries()]
        .filter(([code]) => usedTopics.has(code))
        .map(([code, name]) => ({ value: code, text: name })),
      "All topics",
    );

    const schools = new Map(questions.map((q) => [q.school_code, q.school]));
    optionsFor(
      els.school,
      [...schools.entries()].sort().map(([code, name]) => ({ value: code, text: `${code} — ${name}` })),
      "All schools",
    );

    const years = [...new Set(questions.map((q) => q.year))].sort((a, b) => b - a);
    optionsFor(els.year, years.map((y) => ({ value: y, text: String(y) })), "All years");

    render();
    db.close();
  } catch (err) {
    els.count.textContent = "Failed to load.";
    els.empty.hidden = false;
    els.empty.textContent = String(err);
    console.error(err);
  }
})();
