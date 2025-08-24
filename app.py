import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

from flask import Flask, jsonify, request, Response
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

app = Flask(__name__)

# === Configuration ===
CONFIG_PATH = os.environ.get("ROCKIQ_CONFIG", "config.yml")

yaml = YAML()
yaml.preserve_quotes = True
yaml.width = 4096  # avoid line wraps for long inline structures

# ---------- YAML helpers ----------

def load_all_docs(path: str) -> List[CommentedMap]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p.resolve()}")
    with p.open("r", encoding="utf-8") as f:
        docs = list(yaml.load_all(f))
    return docs

def save_all_docs(path: str, docs: List[CommentedMap]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump_all(docs, f)

def find_doc_by_section(docs: List[CommentedMap], section_name: str) -> Tuple[int, CommentedMap]:
    for idx, d in enumerate(docs):
        if isinstance(d, dict) and d.get("section") == section_name:
            return idx, d
    raise KeyError(f"Section '{section_name}' not found in YAML.")

def get_sequences(seq_doc: CommentedMap) -> List[dict]:
    seqs = seq_doc.get("sequences")
    if not isinstance(seqs, list) or not seqs:
        raise ValueError("No sequences found in SequenceConfig.")
    return seqs

def parse_module_class_func(module_str: str) -> Tuple[str, str]:
    """
    Split 'cAdvanced_PSD.Calculate_PSD' -> ('cAdvanced_PSD', 'Calculate_PSD')
    If no dot present: ('', same_str)
    """
    if not isinstance(module_str, str):
        return "", str(module_str)
    parts = module_str.split(".")
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return "", parts[0]

def build_graph_from_sequence(sequence: dict) -> dict:
    """
    Returns elements for Cytoscape: {'nodes': [...], 'edges': [...]}
    - nodes have data: {id, label, index, cls, func, full, params}
    - edges have data: {id, source, target, label}
    """
    modules = sequence.get("module_sequence", [])
    if not isinstance(modules, list):
        raise ValueError("sequence.module_sequence must be a list.")
    nodes = []
    edges = []
    # Track discovered outputs for each node index
    outputs_by_index: Dict[int, set] = {}

    # Map function name -> list of indices where it appears
    func_to_indices: Dict[str, List[int]] = {}
    # Track most recent node index for each class to connect same-class modules
    cls_to_last_index: Dict[str, int] = {}

    # First pass: create nodes
    for i, mod in enumerate(modules):
        full = mod.get("module", "")
        cls, func = parse_module_class_func(full)
        node_id = f"n{i}"
        # Params shown in editor: everything except 'module'
        params = {k: v for k, v in mod.items() if k != "module"}
        label = f"{func}\n[{cls}]" if cls else func
        nodes.append({
            "data": {
                "id": node_id,
                "label": label,
                "index": i,
                "cls": cls,
                "func": func,
                "full": full,
                "params": params,
            }
        })
        func_to_indices.setdefault(func, []).append(i)

        # Seed outputs with any explicit 'outputs' declared on the node
        try:
            explicit_outputs = mod.get("outputs", {})
            if isinstance(explicit_outputs, dict):
                outputs_by_index.setdefault(i, set()).update(list(explicit_outputs.keys()))
        except Exception:
            pass

        # Create an edge between consecutive nodes of the same class
        if cls:
            prev_idx = cls_to_last_index.get(cls, -1)
            if prev_idx >= 0:
                edges.append({
                    "data": {
                        "id": f"sc{prev_idx}_{i}",
                        "source": f"n{prev_idx}",
                        "target": f"n{i}",
                        "label": "",
                        "edge_type": "same_class",
                    }
                })
            cls_to_last_index[cls] = i

    # Helper to resolve a reference to a prior node by function
    def resolve_ref(current_idx: int, ref_module: str) -> int:
        """
        Try exact function match, then suffix match (take last token after '.'),
        and pick the latest index < current_idx.
        Return -1 if not found.
        """
        target = ref_module or ""
        candidates: List[int] = []

        # Exact function match
        if target in func_to_indices:
            candidates = [idx for idx in func_to_indices[target] if idx < current_idx]

        # Suffix match on last token (e.g. 'cv2.read_image' -> 'read_image')
        if not candidates and "." in target:
            last = target.split(".")[-1]
            if last in func_to_indices:
                candidates = [idx for idx in func_to_indices[last] if idx < current_idx]

        return max(candidates) if candidates else -1

    # Second pass: create edges from ref_* params
    for i, mod in enumerate(modules):
        for k, v in mod.items():
            if not isinstance(k, str) or not k.startswith("ref_"):
                continue
            if isinstance(v, dict):
                ref_mod = v.get("module")
                src_idx = resolve_ref(i, ref_mod)
                if src_idx >= 0:
                    edge_id = f"e{src_idx}_{i}_{k}"
                    label = str(v.get("name", ""))  # output name
                    edges.append({
                        "data": {
                            "id": edge_id,
                            "source": f"n{src_idx}",
                            "target": f"n{i}",
                            "label": label,
                            "edge_type": "input",
                        }
                    })
                    # Record that the source node produces this output name
                    if label:
                        outputs_by_index.setdefault(src_idx, set()).add(label)
            # if list/str etc., ignore—only dicts have module/name/order semantics

    # Attach outputs to node data
    for i, node in enumerate(nodes):
        outs = sorted(list(outputs_by_index.get(i, set())))
        node.get("data", {}).update({"outputs": outs})

    return {"nodes": nodes, "edges": edges}

# ---------- Type coercion for updates ----------

def coerce_value(new_val: Any, old_val: Any) -> Any:
    """
    Convert string input from the UI back into the original type of old_val.
    - If old_val is bool/int/float -> try to cast.
    - If old_val is dict/list/tuple -> expect JSON in text area; try json.loads.
    - Otherwise keep as string (but allow JSON if it parses cleanly).
    """
    # If UI already sent non-str (e.g., checkbox boolean), trust it
    if not isinstance(new_val, str):
        return new_val

    s = new_val.strip()

    # Try to preserve explicit JSON if provided
    if isinstance(old_val, (dict, list, tuple)):
        try:
            parsed = json.loads(s)
            # keep tuple shape if original was tuple
            if isinstance(old_val, tuple) and isinstance(parsed, list):
                return tuple(parsed)
            return parsed
        except Exception:
            # fall through to string if not valid JSON
            return s

    # Booleans
    if isinstance(old_val, bool):
        if s.lower() in ("true", "1", "yes", "on"):
            return True
        if s.lower() in ("false", "0", "no", "off"):
            return False
        return bool(s)

    # Numbers
    if isinstance(old_val, int):
        try:
            return int(s, 10)
        except Exception:
            # maybe float -> int
            try:
                return int(float(s))
            except Exception:
                return old_val
    if isinstance(old_val, float):
        try:
            return float(s)
        except Exception:
            return old_val

    # Try JSON for things like lists written by hand
    try:
        parsed = json.loads(s)
        return parsed
    except Exception:
        pass

    # Strings (including things like "(255,255,255)" that you may want to keep)
    return s

# ---------- Flask endpoints ----------

@app.get("/")
def index() -> Response:
    # Simple inlined page with Cytoscape UI
    html = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>SequenceConfig Graph</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    html, body { height: 100%; margin: 0; font-family: system-ui, Segoe UI, Roboto, Helvetica, Arial, sans-serif; }
    .app { display: grid; grid-template-rows: 56px 1fr; height: 100%; }
    header { display: flex; gap: 12px; align-items: center; padding: 8px 12px; border-bottom: 1px solid #e5e7eb; }
    #cy { width: 100%; height: calc(100vh - 56px); }
    .pill { padding: 6px 10px; border: 1px solid #e5e7eb; border-radius: 9999px; background: #fff; }
    .primary { background: #111827; color: #fff; border-color: #111827; cursor: pointer; }
    .modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.35); display: none; align-items: center; justify-content: center; }
    .modal { width: min(860px, 92vw); max-height: 80vh; overflow: auto; background: #fff; border-radius: 16px; box-shadow: 0 15px 40px rgba(0,0,0,0.25); padding: 16px 18px; }
    .modal h2 { margin: 0 0 10px; font-size: 18px; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .field { display: flex; flex-direction: column; gap: 6px; }
    .field label { font-size: 12px; color: #374151; }
    .field input[type="text"],
    .field input[type="number"],
    .field textarea { border: 1px solid #e5e7eb; border-radius: 10px; padding: 8px 10px; font: inherit; }
    .row { display: flex; gap: 8px; justify-content: flex-end; margin-top: 10px; }
    .muted { color: #6b7280; font-size: 12px; }
    .danger { background: #b91c1c; border-color: #b91c1c; }
    .savebar { position: fixed; left: 0; right: 0; bottom: 0; background: #064e3b; color: #ecfdf5; display: none; align-items: center; justify-content: space-between; padding: 10px 14px; }
    .savebar .pill { background: #065f46; border-color: #10b981; color: #ecfdf5; }
    @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
  </style>
  <script src="https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
  <script src="https://unpkg.com/dagre@0.8.5/dist/dagre.min.js"></script>
  <script src="https://unpkg.com/cytoscape-dagre@2.5.0/cytoscape-dagre.js"></script>
</head>
<body>
  <div class="app">
    <header>
      <strong>SequenceConfig Graph</strong>
      <span class="pill">Config: <code id="cfgName"></code></span>
      <label class="pill">Sequence:
        <select id="sequenceSelect" style="margin-left: 6px; border: none; background: #fff; outline: none;">
        </select>
      </label>
      <button id="refreshBtn" class="pill primary">Refresh</button>
      <span id="status" class="muted"></span>
    </header>
    <div id="cy"></div>
  </div>

  <div id="saveBar" class="savebar">
    <div><strong id="saveMsg">You have pending connections.</strong></div>
    <div class="row" style="margin:0;">
      <button id="discardStagedBtn" class="pill">Discard</button>
      <button id="applyStagedBtn" class="pill primary">Save Changes</button>
    </div>
  </div>

  <div class="modal-backdrop" id="modalBackdrop">
    <div class="modal">
      <h2 id="modalTitle">Edit Node</h2>
      <div class="muted" id="nodeMeta"></div>
      <div class="grid" id="formGrid"></div>
      <div class="row">
        <button id="outputsBtn" class="pill">Outputs</button>
        <button id="closeBtn" class="pill">Close</button>
        <button id="saveBtn" class="pill primary">Save</button>
      </div>
      <div id="outputsPanel" style="display:none; margin-top:10px; border-top:1px solid #e5e7eb; padding-top:10px;">
        <div class="muted" style="margin-bottom:6px;">Drag an output from this node onto another node in the graph to link it, or onto a ref_* field below.</div>
        <div id="outputsList" style="display:flex; flex-wrap: wrap; gap: 8px;"></div>
      </div>
    </div>
  </div>

  <div class="modal-backdrop" id="linkerBackdrop" style="display:none;">
    <div class="modal" style="max-width: 520px;">
      <h2 style="margin-bottom:8px;">Connect Nodes</h2>
      <div class="muted" id="linkerMeta"></div>
      <div class="grid" style="margin-top:10px;">
        <div class="field">
          <label for="linkerSourceOutput">Source output</label>
          <select id="linkerSourceOutput"></select>
        </div>
        <div class="field">
          <label for="linkerTargetInput">Target input (ref_*)</label>
          <select id="linkerTargetInput"></select>
        </div>
      </div>
      <div class="row">
        <button id="linkerCancel" class="pill">Cancel</button>
        <button id="linkerApply" class="pill primary">Connect</button>
      </div>
    </div>
  </div>

<script>
const cfgNameEl = document.getElementById('cfgName');
const seqSelect = document.getElementById('sequenceSelect');
const refreshBtn = document.getElementById('refreshBtn');
const statusEl = document.getElementById('status');

const modalBackdrop = document.getElementById('modalBackdrop');
const modalTitle = document.getElementById('modalTitle');
const nodeMeta = document.getElementById('nodeMeta');
const formGrid = document.getElementById('formGrid');
const closeBtn = document.getElementById('closeBtn');
const saveBtn = document.getElementById('saveBtn');
const outputsBtn = document.getElementById('outputsBtn');
const outputsPanel = document.getElementById('outputsPanel');
const outputsList = document.getElementById('outputsList');
const linkerBackdrop = document.getElementById('linkerBackdrop');
const linkerMeta = document.getElementById('linkerMeta');
const linkerSourceOutput = document.getElementById('linkerSourceOutput');
const linkerTargetInput = document.getElementById('linkerTargetInput');
const linkerCancel = document.getElementById('linkerCancel');
const linkerApply = document.getElementById('linkerApply');
const saveBar = document.getElementById('saveBar');
const saveMsg = document.getElementById('saveMsg');
const applyStagedBtn = document.getElementById('applyStagedBtn');
const discardStagedBtn = document.getElementById('discardStagedBtn');

let cy;
let currentSeqId = null;
let currentNode = null; // cytoscape node
let currentNodeData = null; // its data blob
let stagedLinks = []; // { source_index, source_func, output_name, target_index, target_key }

function escapeHtml(s) {
  return (s ?? '').toString()
    .replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
}

async function loadSequences() {
  const res = await fetch('/sequences');
  if (!res.ok) throw new Error('Failed to load sequences');
  const payload = await res.json();
  cfgNameEl.textContent = payload.config_path;
  seqSelect.innerHTML = '';
  payload.sequences.forEach((s, idx) => {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = `${s.id} — ${s.name ?? 'Sequence ' + s.id}`;
    if (idx === 0) currentSeqId = s.id;
    seqSelect.appendChild(opt);
  });
  if (payload.sequences.length > 0) {
    seqSelect.value = currentSeqId;
  }
}

async function loadGraph() {
  const seq = seqSelect.value ?? currentSeqId ?? 0;
  currentSeqId = seq;
  statusEl.textContent = 'Loading...';
  const res = await fetch(`/graph?sequence=${encodeURIComponent(seq)}`);
  const g = await res.json();

  const elements = [
    ...g.nodes,
    ...g.edges
  ];

  if (!cy) {
    const verticalSpacing = 140;
    cy = cytoscape({
      container: document.getElementById('cy'),
      elements,
      layout: {
        name: 'preset',
        positions: function(node){
          const idx = node.data('index') ?? 0;
          return { x: 0, y: idx * verticalSpacing };
        }
      },
      style: [
        { selector: 'node',
          style: {
            'shape': 'round-rectangle',
            'background-color': '#111827',
            'border-color': '#e5e7eb',
            'border-width': 1,
            'color': '#fff',
            'label': 'data(label)',
            'text-valign': 'center',
            'text-wrap': 'wrap',
            'text-max-width': '180px',
            'font-size': 12,
            'padding': '10px',
            'width': 'label',
            'height': 'label'
          }
        },
        { selector: 'edge',
          style: {
            'curve-style': 'bezier',
            'target-arrow-shape': 'triangle-backcurve',
            'width': 2,
            'line-color': '#9ca3af',
            'target-arrow-color': '#9ca3af',
            'label': 'data(label)',
            'font-size': 10,
            'text-rotation': 'autorotate',
            'text-background-opacity': 1,
            'text-background-color': '#f3f4f6',
            'text-background-padding': 2
          }
        },
        { selector: 'edge[edge_type = "input"]', style: { 'line-color': '#2563eb', 'target-arrow-color': '#2563eb' } },
        { selector: 'edge[edge_type = "same_class"]', style: { 'line-color': '#f97316', 'target-arrow-color': '#f97316', 'line-style': 'dashed' } },
        { selector: 'edge[edge_type = "staged"]', style: { 'line-color': '#10b981', 'target-arrow-color': '#10b981', 'line-style': 'solid' } },
        { selector: 'node:selected', style: { 'background-color': '#2563eb' } }
      ]
    });

    cy.on('tap', 'node', onNodeTap);
    setupCyDnD();
    setupRightDrag();
  } else {
    const verticalSpacing = 140;
    cy.elements().remove();
    cy.add(elements);
    cy.layout({
      name: 'preset',
      positions: function(node){
        const idx = node.data('index') ?? 0;
        return { x: 0, y: idx * verticalSpacing };
      }
    }).run();
  }
  // Re-add staged edges as green overlays
  renderStagedEdges();
  statusEl.textContent = '';
}

function isObject(v) { return v && typeof v === 'object' && !Array.isArray(v); }

function buildField(key, value) {
  const wrap = document.createElement('div');
  wrap.className = 'field';

  const label = document.createElement('label');
  label.htmlFor = `f_${key}`;
  label.textContent = key;
  wrap.appendChild(label);

  // Decide widget type
  if (typeof value === 'boolean') {
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.id = `f_${key}`;
    input.checked = value;
    input.dataset.type = 'bool';
    wrap.appendChild(input);
  } else if (typeof value === 'number') {
    const input = document.createElement('input');
    input.type = 'number';
    input.step = 'any';
    input.id = `f_${key}`;
    input.value = value;
    input.dataset.type = 'number';
    wrap.appendChild(input);
  } else if (Array.isArray(value) || isObject(value)) {
    const ta = document.createElement('textarea');
    ta.rows = 6;
    ta.id = `f_${key}`;
    ta.value = JSON.stringify(value, null, 2);
    ta.dataset.type = 'json';
    // Make ref_* fields droppable
    if (typeof key === 'string' && key.startsWith('ref_')) {
      makeDroppable(ta, key);
    }
    wrap.appendChild(ta);
  } else {
    const input = document.createElement('input');
    input.type = 'text';
    input.id = `f_${key}`;
    input.value = value ?? '';
    input.dataset.type = 'text';
    if (typeof key === 'string' && key.startsWith('ref_')) {
      makeDroppable(input, key);
    }
    wrap.appendChild(input);
  }
  return wrap;
}

function openModal(node) {
  modalBackdrop.style.display = 'flex';
}

function closeModal() {
  modalBackdrop.style.display = 'none';
  currentNode = null;
  currentNodeData = null;
  formGrid.innerHTML = '';
}

function onNodeTap(evt) {
  currentNode = evt.target;
  currentNodeData = currentNode.data();

  modalTitle.textContent = `Edit: ${currentNodeData.func} [${currentNodeData.cls}]`;
  nodeMeta.innerHTML = `<span class="muted">module: <code>${escapeHtml(currentNodeData.full)}</code> — index: ${currentNodeData.index}</span>`;

  formGrid.innerHTML = '';
  const params = currentNodeData.params || {};
  // Show everything (including ref_* and outputs) so you can edit freely
  Object.keys(params).forEach((key) => {
    formGrid.appendChild(buildField(key, params[key]));
  });

  openModal(currentNode);
  // Render outputs for this node only
  renderOutputsPanel();
}

async function saveEdits() {
  if (!currentNodeData) return;
  const seq = currentSeqId;
  const idx = currentNodeData.index;

  // Gather updates
  const updates = {};
  const params = currentNodeData.params || {};
  for (const key of Object.keys(params)) {
    const el = document.getElementById(`f_${key}`);
    if (!el) continue;
    const kind = el.dataset.type;
    let val;
    if (kind === 'bool') {
      val = el.checked;
    } else {
      val = el.value;
    }
    updates[key] = val;
  }

  statusEl.textContent = 'Saving...';
  const res = await fetch('/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sequence_id: seq, node_index: idx, updates })
  });

  if (!res.ok) {
    statusEl.textContent = 'Save failed.';
    const txt = await res.text();
    alert('Save failed: ' + txt);
    return;
  }

  statusEl.textContent = 'Saved.';
  closeModal();
  await loadGraph(); // refresh with updated YAML
}

refreshBtn.addEventListener('click', loadGraph);
closeBtn.addEventListener('click', closeModal);
saveBtn.addEventListener('click', saveEdits);
seqSelect.addEventListener('change', () => { clearStagedLinks(); loadGraph(); });

// ---- Drag & Drop helpers ----
function renderOutputsPanel() {
  if (!cy || !currentNode) return;
  outputsList.innerHTML = '';
  const outs = currentNode.data('outputs') || [];
  outs.forEach(name => {
    const chip = document.createElement('span');
    chip.textContent = name;
    chip.className = 'pill';
    chip.style.cursor = 'grab';
    chip.setAttribute('draggable', 'true');
    chip.addEventListener('dragstart', (e) => {
      const payload = {
        source_index: currentNode.data('index'),
        source_func: currentNode.data('func'),
        output_name: name
      };
      e.dataTransfer.setData('application/json', JSON.stringify(payload));
      e.dataTransfer.effectAllowed = 'copy';
    });
    outputsList.appendChild(chip);
  });
}

function makeDroppable(el, key) {
  el.addEventListener('dragover', (e) => {
    if (hasDnDData(e)) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
      el.style.outline = '2px dashed #2563eb';
    }
  });
  el.addEventListener('dragleave', () => { el.style.outline = ''; });
  el.addEventListener('drop', (e) => {
    const data = getDnDData(e);
    if (!data) return;
    e.preventDefault();
    el.style.outline = '';
    const refObj = {
      module: data.source_func,
      name: data.output_name,
      order: 0
    };
    // If it's a textarea showing json, pretty print
    if (el.tagName === 'TEXTAREA') {
      el.value = JSON.stringify(refObj, null, 2);
      el.dataset.type = 'json';
    } else {
      el.value = JSON.stringify(refObj);
      el.dataset.type = 'json';
    }
  });
}

function hasDnDData(e){
  try { return Array.from(e.dataTransfer.types || []).includes('application/json'); } catch { return false; }
}
function getDnDData(e){
  try { return JSON.parse(e.dataTransfer.getData('application/json')); } catch { return null; }
}

outputsBtn.addEventListener('click', () => {
  const isShown = outputsPanel.style.display !== 'none';
  outputsPanel.style.display = isShown ? 'none' : 'block';
  if (!isShown) renderOutputsPanel();
});

// ---- Drop onto graph to choose mapping ----
let pendingLink = null; // { source_index, source_func, output_name, target_index }
function nodeAtRenderedPoint(rx, ry){
  if (!cy) return null;
  let found = null;
  cy.nodes().forEach(n => {
    const bb = n.renderedBoundingBox();
    if (rx >= bb.x1 && rx <= bb.x2 && ry >= bb.y1 && ry <= bb.y2) {
      found = n;
    }
  });
  return found;
}

function openLinker(source, targetNode){
  pendingLink = { ...source, target_index: targetNode.data('index') };
  // Fill selects
  linkerSourceOutput.innerHTML = '';
  const srcNode = cy.nodes().filter(n => (n.data('index')||-1) === source.source_index)[0];
  const srcOuts = (srcNode && srcNode.data('outputs')) || [];
  srcOuts.forEach(o => {
    const opt = document.createElement('option');
    opt.value = o; opt.textContent = o; linkerSourceOutput.appendChild(opt);
  });
  linkerSourceOutput.value = source.output_name || (srcOuts[0] || '');

  linkerTargetInput.innerHTML = '';
  const params = targetNode.data('params') || {};
  const refKeys = Object.keys(params).filter(k => k.startsWith('ref_'));
  refKeys.forEach(k => {
    const opt = document.createElement('option');
    opt.value = k; opt.textContent = k; linkerTargetInput.appendChild(opt);
  });

  linkerMeta.innerHTML = `Source: <code>${escapeHtml(srcNode ? srcNode.data('func') : source.source_func)}</code> → Target: <code>${escapeHtml(targetNode.data('func'))}</code>`;
  linkerBackdrop.style.display = 'flex';
}

function closeLinker(){
  linkerBackdrop.style.display = 'none';
  pendingLink = null;
}

linkerCancel.addEventListener('click', closeLinker);
linkerApply.addEventListener('click', async () => {
  if (!pendingLink) return;
  const chosenOutput = linkerSourceOutput.value;
  const targetKey = linkerTargetInput.value;
  // Stage (do not persist yet)
  stagedLinks.push({
    source_index: pendingLink.source_index,
    source_func: pendingLink.source_func,
    output_name: chosenOutput,
    target_index: pendingLink.target_index,
    target_key: targetKey
  });
  closeLinker();
  renderStagedEdges();
  updateSaveBarVisibility();
});

// ---- Staged edges rendering and save bar ----
function renderStagedEdges(){
  if (!cy) return;
  // remove previous staged edges
  cy.edges('[edge_type = "staged"]').remove();
  const toAdd = stagedLinks.map((l, i) => ({
    group: 'edges',
    data: {
      id: `stg_${l.source_index}_${l.target_index}_${i}`,
      source: `n${l.source_index}`,
      target: `n${l.target_index}`,
      label: `${l.output_name} → ${l.target_key}`,
      edge_type: 'staged'
    }
  }));
  if (toAdd.length) cy.add(toAdd);
}

function updateSaveBarVisibility(){
  if (stagedLinks.length > 0) {
    saveBar.style.display = 'flex';
    saveMsg.textContent = `You have ${stagedLinks.length} pending connection${stagedLinks.length>1?'s':''}.`;
  } else {
    saveBar.style.display = 'none';
  }
}

discardStagedBtn.addEventListener('click', () => {
  stagedLinks = [];
  renderStagedEdges();
  updateSaveBarVisibility();
});

applyStagedBtn.addEventListener('click', async () => {
  if (!stagedLinks.length) return;
  statusEl.textContent = 'Saving changes...';
  // group updates by target_index
  const groups = {};
  stagedLinks.forEach(l => {
    const key = String(l.target_index);
    groups[key] = groups[key] || { node_index: l.target_index, updates: {} };
    groups[key].updates[l.target_key] = { module: l.source_func, name: l.output_name, order: 0 };
  });
  const payloads = Object.values(groups);
  try {
    const results = await Promise.all(payloads.map(g => fetch('/update', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sequence_id: currentSeqId, node_index: g.node_index, updates: g.updates })
    })));
    const firstBad = results.find(r => !r.ok);
    if (firstBad) {
      const txt = await firstBad.text();
      statusEl.textContent = 'Save failed';
      alert('Save failed: ' + txt);
      return;
    }
    statusEl.textContent = 'Saved';
    stagedLinks = [];
    updateSaveBarVisibility();
    await loadGraph();
  } catch (e) {
    statusEl.textContent = 'Save failed';
    alert('Save failed: ' + e);
  }
});

function clearStagedLinks(){
  stagedLinks = [];
  renderStagedEdges();
  updateSaveBarVisibility();
}

function setupCyDnD(){
  if (!cy) return;
  const container = cy.container();
  container.addEventListener('dragover', (e) => {
    if (hasDnDData(e)) { e.preventDefault(); }
  });
  container.addEventListener('drop', (e) => {
    const data = getDnDData(e);
    if (!data) return;
    e.preventDefault();
    const rect = container.getBoundingClientRect();
    const rx = e.clientX - rect.left; // rendered coords
    const ry = e.clientY - rect.top;
    const target = nodeAtRenderedPoint(rx, ry);
    if (!target) return;
    // If dropping onto the same node, ignore
    if ((target.data('index')||-1) === data.source_index) return;
    openLinker(data, target);
  });
}

// ---- Right-click drag to connect (red arrow overlay) ----
let dragOverlay = null; // { svg, line, onMouseMove, onMouseUp }
function setupRightDrag(){
  if (!cy) return;
  const container = cy.container();
  // Prevent default context menu inside graph area
  container.addEventListener('contextmenu', (e) => e.preventDefault());

  cy.on('cxttapstart', 'node', (evt) => {
    const source = evt.target;
    beginRightDrag(source);
  });
}

function beginRightDrag(sourceNode){
  if (!cy) return;
  endRightDrag();
  const container = cy.container();
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('width', '100%');
  svg.setAttribute('height', '100%');
  svg.style.position = 'absolute';
  svg.style.inset = '0';
  svg.style.pointerEvents = 'none';
  // marker arrowhead
  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
  const marker = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
  marker.setAttribute('id', 'arrowhead');
  marker.setAttribute('markerWidth', '8');
  marker.setAttribute('markerHeight', '8');
  marker.setAttribute('refX', '4');
  marker.setAttribute('refY', '3');
  marker.setAttribute('orient', 'auto');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', 'M0,0 L0,6 L6,3 Z');
  path.setAttribute('fill', '#b91c1c');
  marker.appendChild(path);
  defs.appendChild(marker);
  svg.appendChild(defs);
  const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  line.setAttribute('stroke', '#b91c1c');
  line.setAttribute('stroke-width', '3');
  line.setAttribute('x1', '0'); line.setAttribute('y1', '0');
  line.setAttribute('x2', '0'); line.setAttribute('y2', '0');
  line.setAttribute('marker-end', 'url(#arrowhead)');
  svg.appendChild(line);
  container.style.position = 'relative';
  container.appendChild(svg);

  const update = (clientX, clientY) => {
    const src = sourceNode.renderedPosition();
    line.setAttribute('x1', String(src.x));
    line.setAttribute('y1', String(src.y));
    const rect = container.getBoundingClientRect();
    const rx = clientX - rect.left;
    const ry = clientY - rect.top;
    line.setAttribute('x2', String(rx));
    line.setAttribute('y2', String(ry));
  };

  const onMouseMove = (e) => { update(e.clientX, e.clientY); };
  const onMouseUp = (e) => {
    e.preventDefault();
    const rect = container.getBoundingClientRect();
    const rx = e.clientX - rect.left;
    const ry = e.clientY - rect.top;
    const target = nodeAtRenderedPoint(rx, ry);
    endRightDrag();
    if (target && target.id() !== sourceNode.id()) {
      // Open chooser without preselected output
      openLinker({ source_index: sourceNode.data('index'), source_func: sourceNode.data('func'), output_name: '' }, target);
    }
  };

  document.addEventListener('mousemove', onMouseMove);
  document.addEventListener('mouseup', onMouseUp, { once: true });
  dragOverlay = { svg, line, onMouseMove, onMouseUp };
  // Seed start position
  const lastMouse = cy.renderer().mouseLocation || { x: sourceNode.renderedPosition().x, y: sourceNode.renderedPosition().y };
  update(lastMouse.x || sourceNode.renderedPosition().x, lastMouse.y || sourceNode.renderedPosition().y);
}

function endRightDrag(){
  if (!dragOverlay) return;
  document.removeEventListener('mousemove', dragOverlay.onMouseMove);
  try { dragOverlay.svg.remove(); } catch {}
  dragOverlay = null;
}

// boot
(async function init() {
  try {
    await loadSequences();
    await loadGraph();
  } catch (e) {
    statusEl.textContent = 'Error: ' + e;
  }
})();
</script>
</body>
</html>
    """
    return Response(html, mimetype="text/html")

@app.get("/sequences")
def sequences():
    docs = load_all_docs(CONFIG_PATH)
    _, seq_doc = find_doc_by_section(docs, "SequenceConfig")
    seqs = get_sequences(seq_doc)

    # Build list of {id, name}
    out = []
    for s in seqs:
        out.append({
            "id": s.get("id", 0),
            "name": s.get("name", None)
        })

    return jsonify({
        "config_path": str(Path(CONFIG_PATH).resolve()),
        "sequences": out
    })

@app.get("/graph")
def graph():
    seq_id = request.args.get("sequence", default=None, type=str)
    docs = load_all_docs(CONFIG_PATH)
    _, seq_doc = find_doc_by_section(docs, "SequenceConfig")
    seqs = get_sequences(seq_doc)

    # pick sequence by explicit id string/int if provided, else first
    sequence = None
    if seq_id is not None:
        # try to match either int or string equality on 'id'
        for s in seqs:
            sid = s.get("id")
            if str(sid) == str(seq_id):
                sequence = s
                break
    if sequence is None:
        sequence = seqs[0]

    g = build_graph_from_sequence(sequence)
    return jsonify(g)

@app.post("/update")
def update_node():
    """
    Body: {
      sequence_id: 0,
      node_index: 3,
      updates: { "param": "new value", "filter_size": "13", "simulate": "false", ... }
    }
    """
    data = request.get_json(force=True)
    seq_id = data.get("sequence_id")
    node_index = data.get("node_index")
    updates = data.get("updates", {})

    if node_index is None:
        return Response("node_index is required", status=400)

    docs = load_all_docs(CONFIG_PATH)
    seq_doc_idx, seq_doc = find_doc_by_section(docs, "SequenceConfig")
    seqs = get_sequences(seq_doc)

    # choose sequence
    sequence = None
    sequence_idx = 0
    if seq_id is not None:
        for i, s in enumerate(seqs):
            if str(s.get("id")) == str(seq_id):
                sequence = s
                sequence_idx = i
                break
    if sequence is None:
        sequence = seqs[0]
        sequence_idx = 0

    modules = sequence.get("module_sequence", [])
    if not (0 <= int(node_index) < len(modules)):
        return Response("node_index out of range", status=400)

    node = modules[int(node_index)]
    # Apply updates with type coercion based on existing values
    for k, new_val in updates.items():
        old_val = node.get(k, None)
        node[k] = coerce_value(new_val, old_val)

    # Persist back
    docs[seq_doc_idx]["sequences"][sequence_idx]["module_sequence"][int(node_index)] = node
    save_all_docs(CONFIG_PATH, docs)

    return jsonify({"ok": True, "node_index": node_index})

if __name__ == "__main__":
    app.run(debug=True)
