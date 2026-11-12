import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Activity, AlertTriangle, ArrowUp, Box, Check, Clapperboard, FileText, FolderOpen,
  ImagePlus, Loader2, PanelRight, Plus, Trash2, X,
} from 'lucide-react';
import { api, connect, fileUrl, inTauri, isImage, isVideo } from './api.js';
import Markdown from './markdown.jsx';
import { Doctor, Lightbox, ProductionPanel } from './Panel.jsx';

// The agent's stage names, grouped into the steps a person recognises.
const PIPELINE = [
  { label: 'Brief', stages: ['intake'] },
  { label: 'Concept', stages: ['concept'] },
  { label: 'References', stages: ['references_approved'] },
  { label: 'Shot plan', stages: ['shot_plan_approved'] },
  { label: 'Assets', stages: ['assets_ready'] },
  { label: 'Build', stages: ['building', 'verifying'] },
  { label: 'Preview', stages: ['preview', 'preview_approved'] },
  { label: 'Final', stages: ['delivering', 'delivered'] },
];

const TOOL_LABELS = {
  generate_references: 'Generating concept references',
  show_to_user: 'Showing you',
  register_upload: 'Registering your upload',
  list_references: 'Listing references',
  record_approval: 'Recording approval',
  search_assets: 'Searching free assets',
  download_asset: 'Downloading asset',
  import_inbox: 'Importing inbox files',
  generate_mesh: 'Generating 3D mesh',
  analyze_model: 'Analysing model',
  blender_status: 'Checking Blender',
  blender_run: 'Building in Blender',
  blender_screenshot: 'Taking a verification screenshot',
  blender_render: 'Rendering',
  save_document: 'Saving document',
  project_status: 'Checking project status',
};

const SUGGESTIONS = [
  'A lone antique armchair in a dark studio, warm key light from the left',
  'A small friendly robot character with toy-like proportions, waving',
  'A misty forest clearing at dawn with a slow camera push-in',
];

const KINDS = [
  { id: 'style', label: 'Style reference' },
  { id: 'scene', label: 'Scene reference' },
  { id: 'character', label: 'Character reference' },
];

export const stageLabel = (stage) => (stage || 'intake').replace(/_/g, ' ');

function ago(iso) {
  const then = Date.parse(iso);
  if (!then) return '';
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)}h ago`;
  return `${Math.round(mins / 1440)}d ago`;
}

export default function App() {
  const [phase, setPhase] = useState('connecting');
  const [connectError, setConnectError] = useState('');
  const [info, setInfo] = useState(null);
  const [projects, setProjects] = useState([]);
  const [current, setCurrent] = useState(null);
  const [detail, setDetail] = useState(null);
  const [messages, setMessages] = useState([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [notice, setNotice] = useState('');
  const [lightbox, setLightbox] = useState(null);
  const [doctorOpen, setDoctorOpen] = useState(false);
  const [panelOpen, setPanelOpen] = useState(() => window.innerWidth >= 1180);

  const currentRef = useRef(null);
  const loadedSeq = useRef(null); // null while a project is loading
  const queued = useRef([]);

  const fail = useCallback((err) => setNotice(err?.message || String(err)), []);

  useEffect(() => {
    if (!notice) return undefined;
    const timer = setTimeout(() => setNotice(''), 7000);
    return () => clearTimeout(timer);
  }, [notice]);

  const refreshProjects = useCallback(
    () => api.projects().then(setProjects).catch(() => {}),
    [],
  );

  const refreshState = useCallback(() => {
    const name = currentRef.current;
    if (!name) return;
    api.project(name).then((d) => {
      if (currentRef.current !== name) return;
      const { messages: _ignored, ...rest } = d;
      setDetail(rest);
    }).catch(() => {});
  }, []);

  const applyEvent = useCallback((ev) => {
    if (ev.kind === 'item') {
      setMessages((list) => (list.some((m) => m.id === ev.item.id) ? list : [...list, ev.item]));
    } else if (ev.kind === 'update') {
      setMessages((list) => list.map((m) => (m.id === ev.id ? { ...m, ...ev.patch } : m)));
    } else if (ev.kind === 'busy') {
      setBusy(true);
    } else if (ev.kind === 'done') {
      setBusy(false);
      refreshState();
      refreshProjects();
    } else if (ev.kind === 'state') {
      refreshState();
    }
  }, [refreshProjects, refreshState]);

  const loadProject = useCallback(async (name) => {
    currentRef.current = name;
    loadedSeq.current = null;
    queued.current = [];
    setCurrent(name);
    setLoading(true);
    try {
      localStorage.setItem('scenedirector.project', name);
    } catch {
      // storage is optional
    }
    try {
      const d = await api.project(name);
      if (currentRef.current !== name) return;
      const { messages: history, ...rest } = d;
      setDetail(rest);
      setMessages(history);
      setBusy(d.busy);
      loadedSeq.current = d.seq;
      queued.current.filter((ev) => ev.seq > d.seq).forEach(applyEvent);
      queued.current = [];
    } catch (err) {
      if (currentRef.current === name) {
        setDetail(null);
        setMessages([]);
        fail(err);
      }
    } finally {
      if (currentRef.current === name) setLoading(false);
    }
  }, [applyEvent, fail]);

  // Connect, then keep one event stream open for the life of the window.
  const boot = useCallback(async () => {
    setPhase('connecting');
    try {
      await connect();
    } catch (err) {
      setConnectError(err.message);
      setPhase('offline');
      return undefined;
    }
    setPhase('ready');
    api.info().then(setInfo).catch(() => {});

    const source = api.events();
    let opened = false;
    source.onopen = async () => {
      if (opened) {
        // Reconnected: we may have missed events, so reload what is on screen.
        if (currentRef.current) loadProject(currentRef.current);
        refreshProjects();
        return;
      }
      opened = true;
      const list = await api.projects().catch(() => []);
      setProjects(list);
      let saved = null;
      try {
        saved = localStorage.getItem('scenedirector.project');
      } catch {
        // storage is optional
      }
      const first = list.find((item) => item.name === saved) || list[0];
      if (first) loadProject(first.name);
    };
    source.onmessage = (message) => {
      const ev = JSON.parse(message.data);
      if (ev.kind === 'deleted') {
        refreshProjects();
        return;
      }
      if (ev.project !== currentRef.current) {
        if (ev.kind === 'done' || ev.kind === 'busy') refreshProjects();
        return;
      }
      if (loadedSeq.current === null) queued.current.push(ev);
      else if (ev.seq > loadedSeq.current) applyEvent(ev);
    };
    return () => source.close();
  }, [applyEvent, loadProject, refreshProjects]);

  useEffect(() => {
    let cleanup;
    let cancelled = false;
    boot().then((fn) => {
      if (cancelled) fn?.();
      else cleanup = fn;
    });
    return () => {
      cancelled = true;
      cleanup?.();
    };
    // boot once per mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const createProject = async (name) => {
    const created = await api.createProject(name);
    await refreshProjects();
    loadProject(created.name);
  };

  const deleteProject = async (name) => {
    await api.deleteProject(name);
    const list = await api.projects().catch(() => []);
    setProjects(list);
    if (currentRef.current !== name) return;
    if (list[0]) {
      loadProject(list[0].name);
    } else {
      currentRef.current = null;
      setCurrent(null);
      setDetail(null);
      setMessages([]);
      setBusy(false);
    }
  };

  const send = async (text, attachments) => {
    setBusy(true);
    try {
      await api.chat(current, text, attachments.map((a) => a.path));
      return true;
    } catch (err) {
      setBusy(false);
      fail(err);
      return false;
    }
  };

  const openPath = (path, app) => api.open(current, path, app).catch(fail);

  if (phase !== 'ready') {
    return <Splash phase={phase} error={connectError} onRetry={boot} />;
  }

  return (
    <div className={`app${panelOpen && detail ? ' with-panel' : ''}`}>
      <Sidebar
        projects={projects}
        current={current}
        info={info}
        onSelect={loadProject}
        onCreate={createProject}
        onDelete={deleteProject}
        onOpenFolder={(name) => api.open(name, '').catch(fail)}
        onDoctor={() => setDoctorOpen(true)}
        fail={fail}
      />

      <main className="stage">
        {detail ? (
          <>
            <header className="topbar">
              <div className="topbar-title">
                <h1>{detail.name}</h1>
                <span className="stage-chip">{stageLabel(detail.stage)}</span>
              </div>
              <div className="topbar-actions">
                {detail.has_blend && (
                  <button
                    type="button" className="btn ghost" disabled={busy}
                    title={busy ? 'Available when the director has finished' : 'Open scene.blend in Blender'}
                    onClick={() => openPath('', 'blender')}
                  >
                    <Box size={15} /> Open in Blender
                  </button>
                )}
                <button type="button" className="btn ghost" onClick={() => openPath('')}>
                  <FolderOpen size={15} /> Folder
                </button>
                <button
                  type="button" className={`btn ghost icon${panelOpen ? ' on' : ''}`}
                  aria-label="Toggle production panel" aria-pressed={panelOpen}
                  onClick={() => setPanelOpen((open) => !open)}
                >
                  <PanelRight size={16} />
                </button>
              </div>
            </header>
            <Pipeline stage={detail.stage} />
            <Conversation
              project={detail.name}
              messages={messages}
              busy={busy}
              loading={loading}
              onOpenMedia={(items, index) => setLightbox({ items, index })}
              onOpenFile={openPath}
            />
            <Composer
              key={detail.name}
              project={detail.name}
              busy={busy}
              empty={messages.length === 0}
              onSend={send}
              fail={fail}
            />
          </>
        ) : (
          <Welcome loading={loading || (projects.length > 0 && !current)} onCreate={createProject} fail={fail} />
        )}
      </main>

      {detail && panelOpen && (
        <ProductionPanel
          detail={detail}
          busy={busy}
          onChanged={refreshState}
          onOpenMedia={(items, index) => setLightbox({ items, index })}
          onOpenFile={openPath}
          onClose={() => setPanelOpen(false)}
          fail={fail}
        />
      )}

      {lightbox && detail && (
        <Lightbox
          project={detail.name}
          items={lightbox.items}
          index={lightbox.index}
          onIndex={(index) => setLightbox((box) => ({ ...box, index }))}
          onClose={() => setLightbox(null)}
          onOpenFile={openPath}
        />
      )}
      {doctorOpen && <Doctor project={current} onClose={() => setDoctorOpen(false)} />}
      {notice && (
        <div className="notice" role="alert">
          <AlertTriangle size={15} />
          <span>{notice}</span>
          <button type="button" aria-label="Dismiss" onClick={() => setNotice('')}><X size={14} /></button>
        </div>
      )}
    </div>
  );
}

function Splash({ phase, error, onRetry }) {
  return (
    <div className="splash">
      <Clapperboard size={30} strokeWidth={1.5} />
      {phase === 'connecting' ? (
        <>
          <h1>Starting SceneDirector</h1>
          <p><Loader2 size={14} className="spin" /> Waiting for the Python backend</p>
        </>
      ) : (
        <>
          <h1>The backend is not running</h1>
          <p>{error}</p>
          {!inTauri() && (
            <p>Start it from the project folder with <code>python -m directorc.server</code></p>
          )}
          <button type="button" className="btn primary" onClick={onRetry}>Try again</button>
        </>
      )}
    </div>
  );
}

function NewProject({ onCreate, fail, autoFocus }) {
  const [name, setName] = useState('');
  const [working, setWorking] = useState(false);
  const submit = async (event) => {
    event.preventDefault();
    if (!name.trim() || working) return;
    setWorking(true);
    try {
      await onCreate(name.trim());
      setName('');
    } catch (err) {
      fail(err);
    } finally {
      setWorking(false);
    }
  };
  return (
    <form className="new-project" onSubmit={submit}>
      <input
        value={name} onChange={(e) => setName(e.target.value)} maxLength={40}
        placeholder="New project name" aria-label="New project name" autoFocus={autoFocus}
      />
      <button type="submit" className="btn icon" disabled={!name.trim() || working} aria-label="Create project">
        {working ? <Loader2 size={15} className="spin" /> : <Plus size={15} />}
      </button>
    </form>
  );
}

function Sidebar({ projects, current, info, onSelect, onCreate, onDelete, onOpenFolder, onDoctor, fail }) {
  const [menu, setMenu] = useState(null); // { name, x, y }
  const [confirm, setConfirm] = useState(null); // project name awaiting confirmation
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    if (!menu && !confirm) return undefined;
    const onKey = (event) => {
      if (event.key !== 'Escape' || deleting) return;
      setMenu(null);
      setConfirm(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [menu, confirm, deleting]);

  const openMenu = (event, item) => {
    event.preventDefault();
    setMenu({
      name: item.name,
      busy: item.busy,
      x: Math.min(event.clientX, window.innerWidth - 200),
      y: Math.min(event.clientY, window.innerHeight - 96),
    });
  };

  const runDelete = async () => {
    setDeleting(true);
    try {
      await onDelete(confirm);
      setConfirm(null);
    } catch (err) {
      fail(err);
    } finally {
      setDeleting(false);
    }
  };

  return (
    <aside className="sidebar">
      {menu && (
        <>
          <div className="menu-scrim" onClick={() => setMenu(null)} onContextMenu={(e) => { e.preventDefault(); setMenu(null); }} />
          <div className="menu context" role="menu" style={{ left: menu.x, top: menu.y }}>
            <button type="button" role="menuitem" onClick={() => { onOpenFolder(menu.name); setMenu(null); }}>
              <FolderOpen size={14} /> Open folder
            </button>
            <button
              type="button" role="menuitem" className="danger" disabled={menu.busy}
              title={menu.busy ? 'The director is working in this project' : undefined}
              onClick={() => { setConfirm(menu.name); setMenu(null); }}
            >
              <Trash2 size={14} /> Delete project
            </button>
          </div>
        </>
      )}
      {confirm && (
        <div className="scrim" onClick={() => !deleting && setConfirm(null)}>
          <div className="dialog confirm" role="alertdialog" aria-modal="true" aria-label="Delete project" onClick={(e) => e.stopPropagation()}>
            <h2>Delete “{confirm}”?</h2>
            <p>
              The whole project folder moves to the Recycle Bin: the Blender scene, references,
              downloaded assets, renders and the conversation. You can restore it from there.
            </p>
            <footer>
              <button type="button" className="btn" disabled={deleting} autoFocus onClick={() => setConfirm(null)}>Cancel</button>
              <button type="button" className="btn danger" disabled={deleting} onClick={runDelete}>
                {deleting ? <Loader2 size={14} className="spin" /> : <Trash2 size={14} />} Delete project
              </button>
            </footer>
          </div>
        </div>
      )}
      <div className="brand">
        <Clapperboard size={18} strokeWidth={1.75} />
        <span>SceneDirector</span>
      </div>
      <NewProject onCreate={onCreate} fail={fail} />
      <nav className="projects" aria-label="Projects">
        {projects.length === 0 && <p className="hint">No projects yet. Name one above to start.</p>}
        {projects.map((item) => (
          <button
            type="button" key={item.name}
            className={`project${item.name === current ? ' active' : ''}`}
            aria-current={item.name === current ? 'page' : undefined}
            onClick={() => item.name !== current && onSelect(item.name)}
            onContextMenu={(event) => openMenu(event, item)}
          >
            <span className="project-cover">
              {item.cover
                ? <img src={fileUrl(item.name, item.cover)} alt="" loading="lazy" />
                : <Clapperboard size={15} strokeWidth={1.5} />}
            </span>
            <span className="project-text">
              <span className="project-name">{item.name}</span>
              <span className="project-meta">
                {item.busy ? 'working…' : stageLabel(item.stage)}
                {item.updated && !item.busy ? ` · ${ago(item.updated)}` : ''}
              </span>
            </span>
            {item.busy && <Loader2 size={13} className="spin" />}
          </button>
        ))}
      </nav>
      <div className="env">
        {info && (
          <dl>
            <div>
              <dt>Model</dt>
              <dd title={info.endpoint}>{info.model}</dd>
            </div>
            <div>
              <dt>Blender</dt>
              <dd className={info.blender ? '' : 'bad'} title={info.blender || ''}>
                {info.blender ? 'found' : 'not found'}
              </dd>
            </div>
          </dl>
        )}
        <button type="button" className="btn ghost wide" onClick={onDoctor}>
          <Activity size={15} /> Diagnostics
        </button>
      </div>
    </aside>
  );
}

function Pipeline({ stage }) {
  const active = PIPELINE.findIndex((step) => step.stages.includes(stage));
  return (
    <ol className="pipeline" aria-label="Production stage">
      {PIPELINE.map((step, index) => {
        const state = active < 0 ? 'todo' : index < active ? 'done' : index === active ? 'now' : 'todo';
        return (
          <li key={step.label} className={state} aria-current={state === 'now' ? 'step' : undefined}>
            <span className="dot">{state === 'done' && <Check size={10} strokeWidth={3} />}</span>
            {step.label}
          </li>
        );
      })}
    </ol>
  );
}

function Welcome({ loading, onCreate, fail }) {
  if (loading) {
    return <div className="welcome"><Loader2 size={18} className="spin" /></div>;
  }
  return (
    <div className="welcome">
      <Clapperboard size={34} strokeWidth={1.25} />
      <h1>Start a project</h1>
      <p>
        Each project is a Blender scene with its references, assets and renders.
        Name one, then describe what you want to see.
      </p>
      <NewProject onCreate={onCreate} fail={fail} autoFocus />
    </div>
  );
}

function MediaStrip({ project, media, onOpenMedia, onOpenFile }) {
  if (!media?.length) return null;
  const visual = media.filter((m) => isImage(m.path) || isVideo(m.path));
  const files = media.filter((m) => !isImage(m.path) && !isVideo(m.path));
  return (
    <>
      {visual.length > 0 && (
        <div className={`media-strip count-${Math.min(visual.length, 4)}`}>
          {visual.map((item, index) => (
            <button
              type="button" key={item.path} className="thumb" title={item.name}
              onClick={() => onOpenMedia(visual, index)}
            >
              {isVideo(item.path)
                ? <video src={fileUrl(project, item)} muted preload="metadata" />
                : <img src={fileUrl(project, item)} alt={item.name} loading="lazy" />}
              {isVideo(item.path) && <span className="badge">video</span>}
            </button>
          ))}
        </div>
      )}
      {files.map((item) => (
        <button type="button" key={item.path} className="file-chip" onClick={() => onOpenFile(item.path)}>
          <FileText size={14} /> {item.path}
        </button>
      ))}
    </>
  );
}

function ToolRow({ project, item, onOpenMedia, onOpenFile }) {
  const failed = !item.running && item.ok === false;
  return (
    <div className={`tool${failed ? ' failed' : ''}`}>
      <div className="tool-line">
        <span className="tool-icon">
          {item.running ? <Loader2 size={13} className="spin" />
            : failed ? <X size={13} strokeWidth={2.5} /> : <Check size={13} strokeWidth={2.5} />}
        </span>
        <span className="tool-name">{TOOL_LABELS[item.name] || item.name}</span>
        {item.summary && <span className="tool-summary">{item.summary}</span>}
      </div>
      {item.running && item.progress && <div className="tool-progress">{item.progress}</div>}
      {failed && item.error && <div className="tool-error">{item.error}</div>}
      <MediaStrip project={project} media={item.media} onOpenMedia={onOpenMedia} onOpenFile={onOpenFile} />
    </div>
  );
}

function Conversation({ project, messages, busy, loading, onOpenMedia, onOpenFile }) {
  const scroller = useRef(null);
  const pinned = useRef(true);

  useEffect(() => {
    pinned.current = true;
  }, [project]);

  useEffect(() => {
    const el = scroller.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [messages, busy, project]);

  const onScroll = () => {
    const el = scroller.current;
    pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const last = messages[messages.length - 1];
  const thinking = busy && !(last?.type === 'tool' && last.running);

  return (
    <div className="conversation" ref={scroller} onScroll={onScroll}>
      <div className="conversation-inner">
        {loading && messages.length === 0 && (
          <div className="thinking"><Loader2 size={14} className="spin" /> Loading project</div>
        )}
        {!loading && messages.length === 0 && !busy && (
          <div className="empty">
            <h2>Describe the scene or character you want</h2>
            <p>
              The director asks a few questions, shows concept references for your approval,
              then builds, lights and renders the scene in Blender.
            </p>
          </div>
        )}
        {messages.map((item) => {
          if (item.type === 'user') {
            return (
              <div key={item.id} className="msg user">
                <div className="bubble">
                  {item.text}
                  {item.attached && !item.media?.length && <span className="attached-note">image attached</span>}
                </div>
                <MediaStrip project={project} media={item.media} onOpenMedia={onOpenMedia} onOpenFile={onOpenFile} />
              </div>
            );
          }
          if (item.type === 'assistant') {
            return (
              <div key={item.id} className="msg director">
                <div className="who">Director</div>
                <Markdown text={item.text} />
              </div>
            );
          }
          if (item.type === 'tool') {
            return <ToolRow key={item.id} project={project} item={item} onOpenMedia={onOpenMedia} onOpenFile={onOpenFile} />;
          }
          return (
            <div key={item.id} className="msg error" role="alert">
              <AlertTriangle size={16} />
              <div>
                <strong>{item.title}</strong>
                <p>{item.text}</p>
                {item.hint && <p className="hint">{item.hint}</p>}
              </div>
            </div>
          );
        })}
        {thinking && (
          <div className="thinking"><Loader2 size={14} className="spin" /> Director is thinking</div>
        )}
      </div>
    </div>
  );
}

function Composer({ project, busy, empty, onSend, fail }) {
  const [text, setText] = useState('');
  const [attachments, setAttachments] = useState([]);
  const [uploading, setUploading] = useState(0);
  const [menu, setMenu] = useState(false);
  const [dragging, setDragging] = useState(false);
  const kind = useRef('style');
  const picker = useRef(null);
  const area = useRef(null);

  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
  }, [text]);

  const upload = useCallback(async (files, as) => {
    const images = [...files].filter((file) => isImage(file.name));
    if (images.length < files.length) fail(new Error('Only PNG, JPG, WEBP or BMP images can be attached.'));
    for (const file of images) {
      setUploading((n) => n + 1);
      try {
        const ref = await api.upload(project, file, as);
        setAttachments((list) => [...list, ref]);
      } catch (err) {
        fail(err);
      } finally {
        setUploading((n) => n - 1);
      }
    }
  }, [fail, project]);

  // Dropping an image anywhere on the window attaches it as a style reference.
  useEffect(() => {
    const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes('Files');
    const over = (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      setDragging(true);
    };
    const leave = (event) => {
      if (!event.relatedTarget) setDragging(false);
    };
    const drop = (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      setDragging(false);
      upload(event.dataTransfer.files, 'style');
    };
    window.addEventListener('dragover', over);
    window.addEventListener('dragleave', leave);
    window.addEventListener('drop', drop);
    return () => {
      window.removeEventListener('dragover', over);
      window.removeEventListener('dragleave', leave);
      window.removeEventListener('drop', drop);
    };
  }, [upload]);

  const canSend = text.trim() && !busy && uploading === 0;

  const submit = async () => {
    if (!canSend) return;
    const draft = text;
    const sent = attachments;
    setText('');
    setAttachments([]);
    const ok = await onSend(draft.trim(), sent);
    if (!ok) {
      setText(draft);
      setAttachments(sent);
    }
  };

  const pick = (as) => {
    kind.current = as;
    setMenu(false);
    picker.current.click();
  };

  return (
    <div className="composer-wrap">
      {empty && !busy && !text && (
        <div className="suggestions">
          {SUGGESTIONS.map((line) => (
            <button type="button" key={line} onClick={() => { setText(line); area.current?.focus(); }}>
              {line}
            </button>
          ))}
        </div>
      )}
      <div className={`composer${dragging ? ' dragging' : ''}`}>
        {(attachments.length > 0 || uploading > 0) && (
          <div className="pending">
            {attachments.map((item) => (
              <span key={item.path} className="pending-item">
                <img src={fileUrl(project, item)} alt="" />
                <span>{item.kind}</span>
                <button
                  type="button" aria-label={`Remove ${item.name}`}
                  onClick={() => setAttachments((list) => list.filter((a) => a.path !== item.path))}
                >
                  <X size={12} />
                </button>
              </span>
            ))}
            {uploading > 0 && <span className="pending-item"><Loader2 size={13} className="spin" /> uploading</span>}
          </div>
        )}
        <textarea
          ref={area} rows={1} value={text}
          placeholder={busy ? 'The director is working. You can write your next note meanwhile.' : 'Describe a scene, answer the director, or approve a reference'}
          aria-label="Message the director"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <div className="composer-bar">
          <div className="attach">
            <button
              type="button" className="btn ghost" aria-haspopup="menu" aria-expanded={menu}
              onClick={() => setMenu((open) => !open)}
            >
              <ImagePlus size={15} /> Attach reference
            </button>
            {menu && (
              <>
                <div className="menu-scrim" onClick={() => setMenu(false)} />
                <div className="menu" role="menu">
                  {KINDS.map((option) => (
                    <button type="button" role="menuitem" key={option.id} onClick={() => pick(option.id)}>
                      {option.label}
                    </button>
                  ))}
                </div>
              </>
            )}
            <input
              ref={picker} type="file" accept="image/png,image/jpeg,image/webp,image/bmp" multiple hidden
              onChange={(e) => { upload(e.target.files, kind.current); e.target.value = ''; }}
            />
          </div>
          <span className="composer-hint">{dragging ? 'Drop to attach as a style reference' : 'Enter to send · Shift+Enter for a new line'}</span>
          <button type="button" className="btn primary send" disabled={!canSend} onClick={submit} aria-label="Send">
            {busy ? <Loader2 size={16} className="spin" /> : <ArrowUp size={16} strokeWidth={2.5} />}
          </button>
        </div>
      </div>
    </div>
  );
}
