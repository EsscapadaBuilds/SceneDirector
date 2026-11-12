import React, { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle, Check, ChevronLeft, ChevronRight, ExternalLink, FileText, Inbox,
  Loader2, RefreshCw, X,
} from 'lucide-react';
import { api, fileUrl, isImage, isVideo } from './api.js';

const GATES = [
  { id: 'scene_reference', label: 'Scene reference', note: 'The look of the scene is agreed' },
  { id: 'character_reference', label: 'Character reference', note: 'The character design is agreed' },
  { id: 'shot_plan', label: 'Shot plan', note: 'Camera, framing and motion are agreed' },
  { id: 'assets', label: 'Assets', note: 'Models, HDRIs and textures are ready' },
  { id: 'preview', label: 'Preview', note: 'The preview render is good to finalise' },
  { id: 'final', label: 'Final', note: 'The delivered render is accepted' },
];

const size = (bytes) => (bytes > 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1e3))} KB`);

function Grid({ project, items, onOpenMedia, caption }) {
  return (
    <div className="grid">
      {items.map((item, index) => (
        <button
          type="button" key={item.path} className="thumb" title={item.name || item.path}
          onClick={() => onOpenMedia(items, index)}
        >
          {isVideo(item.path)
            ? <video src={fileUrl(project, item)} muted preload="metadata" />
            : <img src={fileUrl(project, item)} alt={item.name || item.path} loading="lazy" />}
          {isVideo(item.path) && <span className="badge">video</span>}
          {caption?.(item)}
        </button>
      ))}
    </div>
  );
}

function Section({ title, count, children, empty }) {
  return (
    <section className="section">
      <h3>{title}{count > 0 && <span className="count">{count}</span>}</h3>
      {count > 0 ? children : <p className="hint">{empty}</p>}
    </section>
  );
}

function MediaTab({ detail, onOpenMedia, onOpenFile }) {
  const refs = detail.references.filter((r) => !r.missing && isImage(r.path));
  return (
    <>
      <Section title="Output" count={detail.output.length} empty="Final stills and animations land here.">
        <Grid project={detail.name} items={detail.output} onOpenMedia={onOpenMedia} />
      </Section>
      <Section title="References" count={refs.length} empty="Concept images the director generates, and images you attach.">
        <Grid
          project={detail.name} items={refs} onOpenMedia={onOpenMedia}
          caption={(ref) => (
            <span className={`badge ${ref.approved ? 'ok' : ''}`}>
              {ref.approved && <Check size={10} strokeWidth={3} />}{ref.kind}
            </span>
          )}
        />
      </Section>
      <Section title="Renders" count={detail.renders.length} empty="Verification screenshots and previews appear as the scene is built.">
        <Grid project={detail.name} items={detail.renders} onOpenMedia={onOpenMedia} />
      </Section>
      {detail.docs.length > 0 && (
        <Section title="Documents" count={detail.docs.length}>
          <ul className="rows">
            {detail.docs.map((doc) => (
              <li key={doc.path}>
                <button type="button" className="row-button" onClick={() => onOpenFile(doc.path)}>
                  <FileText size={14} /> {doc.name}
                </button>
              </li>
            ))}
          </ul>
        </Section>
      )}
    </>
  );
}

function AssetsTab({ detail, busy, onChanged, onOpenFile, fail }) {
  const [importing, setImporting] = useState(false);
  const runImport = async () => {
    setImporting(true);
    try {
      await api.importInbox(detail.name);
      onChanged();
    } catch (err) {
      fail(err);
    } finally {
      setImporting(false);
    }
  };
  return (
    <>
      <Section title="In the project" count={detail.assets.length} empty="Free CC0 models, HDRIs and textures the director downloads are listed here with their licence.">
        <ul className="rows">
          {detail.assets.map((asset) => (
            <li key={asset.path} className="asset">
              <span className="asset-kind">{asset.kind}</span>
              <span className="asset-name" title={asset.path}>{asset.name}</span>
              <span className="asset-meta">{asset.source} · {asset.license}</span>
            </li>
          ))}
        </ul>
      </Section>
      <section className="section">
        <h3>Inbox{detail.inbox.length > 0 && <span className="count">{detail.inbox.length}</span>}</h3>
        <p className="hint">
          Drop your own models or textures into the inbox folder, then register them so the
          director can use them.
        </p>
        {detail.inbox.length > 0 && (
          <ul className="rows">
            {detail.inbox.map((file) => (
              <li key={file.name} className="asset">
                <span className="asset-name">{file.name}</span>
                <span className="asset-meta">{size(file.size)}{file.new ? ' · new' : ''}</span>
              </li>
            ))}
          </ul>
        )}
        <div className="section-actions">
          <button type="button" className="btn" onClick={() => onOpenFile('assets/inbox')}>
            <Inbox size={14} /> Open inbox folder
          </button>
          {detail.inbox.length > 0 && (
            <button type="button" className="btn" disabled={importing || busy} onClick={runImport}>
              {importing ? <Loader2 size={14} className="spin" /> : <Check size={14} />} Register files
            </button>
          )}
        </div>
      </section>
    </>
  );
}

function ApprovalsTab({ detail, onChanged, fail }) {
  const [working, setWorking] = useState('');
  const known = new Set(GATES.map((g) => g.id));
  const extra = Object.keys(detail.gates).filter((id) => !known.has(id))
    .map((id) => ({ id, label: id.replace(/_/g, ' '), note: '' }));

  const set = async (stage, approved) => {
    setWorking(stage);
    try {
      await api.setGate(detail.name, stage, approved);
      onChanged();
    } catch (err) {
      fail(err);
    } finally {
      setWorking('');
    }
  };

  return (
    <section className="section">
      <p className="hint">
        The director records these when you approve something in the conversation.
        You can also set them by hand.
      </p>
      <ul className="gates">
        {[...GATES, ...extra].map((gate) => {
          const state = detail.gates[gate.id];
          const approved = Boolean(state?.approved);
          return (
            <li key={gate.id} className={approved ? 'approved' : ''}>
              <span className="gate-mark">{approved && <Check size={12} strokeWidth={3} />}</span>
              <span className="gate-text">
                <span className="gate-label">{gate.label}</span>
                <span className="gate-note">
                  {approved
                    ? `Approved${state.notes ? `: ${state.notes}` : ''}`
                    : state ? 'Approval withdrawn' : gate.note}
                </span>
              </span>
              <button
                type="button" className="btn small" disabled={working === gate.id}
                onClick={() => set(gate.id, !approved)}
              >
                {approved ? 'Withdraw' : 'Approve'}
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export function ProductionPanel({ detail, busy, onChanged, onOpenMedia, onOpenFile, onClose, fail }) {
  const [tab, setTab] = useState('media');
  const approved = Object.values(detail.gates).filter((g) => g.approved).length;
  const tabs = [
    { id: 'media', label: 'Media' },
    { id: 'assets', label: 'Assets', count: detail.assets.length + detail.inbox.filter((f) => f.new).length },
    { id: 'approvals', label: 'Approvals', count: approved },
  ];
  return (
    <aside className="panel" aria-label="Production">
      <div className="tabs" role="tablist">
        {tabs.map((item) => (
          <button
            type="button" key={item.id} role="tab" aria-selected={tab === item.id}
            className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)}
          >
            {item.label}{item.count > 0 && <span className="count">{item.count}</span>}
          </button>
        ))}
        <button type="button" className="panel-close" aria-label="Close panel" onClick={onClose}>
          <X size={15} />
        </button>
      </div>
      <div className="panel-body">
        {tab === 'media' && <MediaTab detail={detail} onOpenMedia={onOpenMedia} onOpenFile={onOpenFile} />}
        {tab === 'assets' && <AssetsTab detail={detail} busy={busy} onChanged={onChanged} onOpenFile={onOpenFile} fail={fail} />}
        {tab === 'approvals' && <ApprovalsTab detail={detail} onChanged={onChanged} fail={fail} />}
      </div>
    </aside>
  );
}

export function Lightbox({ project, items, index, onIndex, onClose, onOpenFile }) {
  const item = items[index];
  const step = useCallback(
    (delta) => onIndex((index + delta + items.length) % items.length),
    [index, items.length, onIndex],
  );

  useEffect(() => {
    const onKey = (event) => {
      if (event.key === 'Escape') onClose();
      else if (event.key === 'ArrowRight' && items.length > 1) step(1);
      else if (event.key === 'ArrowLeft' && items.length > 1) step(-1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [items.length, onClose, step]);

  if (!item) return null;
  return (
    <div className="lightbox" role="dialog" aria-modal="true" aria-label={item.name || item.path} onClick={onClose}>
      <div className="lightbox-bar" onClick={(e) => e.stopPropagation()}>
        <span className="lightbox-name">{item.path}</span>
        {items.length > 1 && <span className="lightbox-count">{index + 1} / {items.length}</span>}
        <button type="button" className="btn ghost" onClick={() => onOpenFile(item.path)}>
          <ExternalLink size={14} /> Open in default app
        </button>
        <button type="button" className="btn ghost icon" aria-label="Close" autoFocus onClick={onClose}>
          <X size={16} />
        </button>
      </div>
      <div className="lightbox-stage">
        {items.length > 1 && (
          <button type="button" className="nav prev" aria-label="Previous" onClick={(e) => { e.stopPropagation(); step(-1); }}>
            <ChevronLeft size={22} />
          </button>
        )}
        {isVideo(item.path)
          ? <video key={item.path} src={fileUrl(project, item)} controls autoPlay loop onClick={(e) => e.stopPropagation()} />
          : <img src={fileUrl(project, item)} alt={item.name || item.path} onClick={(e) => e.stopPropagation()} />}
        {items.length > 1 && (
          <button type="button" className="nav next" aria-label="Next" onClick={(e) => { e.stopPropagation(); step(1); }}>
            <ChevronRight size={22} />
          </button>
        )}
      </div>
      {item.prompt && <p className="lightbox-caption" onClick={(e) => e.stopPropagation()}>{item.prompt}</p>}
    </div>
  );
}

export function Doctor({ project, onClose }) {
  const [checks, setChecks] = useState(null);
  const [error, setError] = useState('');

  const run = useCallback(() => {
    setChecks(null);
    setError('');
    api.doctor(project).then((d) => setChecks(d.checks)).catch((err) => setError(err.message));
  }, [project]);

  useEffect(run, [run]);
  useEffect(() => {
    const onKey = (event) => event.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="scrim" onClick={onClose}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label="Diagnostics" onClick={(e) => e.stopPropagation()}>
        <header>
          <h2>Diagnostics</h2>
          <button type="button" className="btn ghost icon" aria-label="Close" onClick={onClose}><X size={16} /></button>
        </header>
        {!checks && !error && (
          <p className="dialog-wait"><Loader2 size={15} className="spin" /> Testing the model, Blender and the image and asset services</p>
        )}
        {error && <p className="dialog-wait bad"><AlertTriangle size={15} /> {error}</p>}
        {checks && (
          <ul className="checks">
            {checks.map((check) => (
              <li key={check.label} className={check.status}>
                <span className="check-mark">
                  {check.status === 'ok' ? <Check size={12} strokeWidth={3} />
                    : check.status === 'warn' ? '!' : <X size={12} strokeWidth={3} />}
                </span>
                <span className="check-label">{check.label}</span>
                <span className="check-detail">{check.detail}</span>
              </li>
            ))}
          </ul>
        )}
        <footer>
          <span className="hint">Settings live in the .env file next to cli.py.</span>
          <button type="button" className="btn" disabled={!checks && !error} onClick={run}>
            <RefreshCw size={14} /> Run again
          </button>
        </footer>
      </div>
    </div>
  );
}
