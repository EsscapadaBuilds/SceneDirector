import React from 'react';

// A small, safe Markdown renderer for the director's replies: it builds React
// elements directly, so model output is never injected as HTML.

function inline(text, keyBase) {
  const out = [];
  const pattern = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\s][^*\n]*\*)|(\[[^\]\n]+\]\([^)\s]+\))/g;
  let last = 0;
  let match;
  let n = 0;
  while ((match = pattern.exec(text))) {
    if (match.index > last) out.push(text.slice(last, match.index));
    const token = match[0];
    const key = `${keyBase}-${n++}`;
    if (match[1]) out.push(<code key={key}>{token.slice(1, -1)}</code>);
    else if (match[2]) out.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    else if (match[3]) out.push(<em key={key}>{token.slice(1, -1)}</em>);
    else out.push(token.slice(1, token.indexOf(']')));
    last = match.index + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export default function Markdown({ text }) {
  const lines = String(text || '').replace(/\r\n/g, '\n').split('\n');
  const blocks = [];
  let i = 0;
  const key = () => `b${blocks.length}`;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
      continue;
    }
    if (line.trim().startsWith('```')) {
      const code = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith('```')) code.push(lines[i++]);
      i += 1;
      blocks.push(<pre key={key()}><code>{code.join('\n')}</code></pre>);
      continue;
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push(<h4 key={key()}>{inline(heading[2], key())}</h4>);
      i += 1;
      continue;
    }
    const bullet = /^\s*[-*•]\s+/;
    const numbered = /^\s*\d+[.)]\s+/;
    if (bullet.test(line) || numbered.test(line)) {
      const marker = bullet.test(line) ? bullet : numbered;
      const Tag = marker === bullet ? 'ul' : 'ol';
      const items = [];
      while (i < lines.length && marker.test(lines[i])) {
        items.push(lines[i].replace(marker, ''));
        i += 1;
      }
      const k = key();
      blocks.push(
        <Tag key={k}>{items.map((item, n) => <li key={n}>{inline(item, `${k}-${n}`)}</li>)}</Tag>,
      );
      continue;
    }
    const para = [];
    while (
      i < lines.length && lines[i].trim() && !lines[i].trim().startsWith('```')
      && !/^(#{1,4})\s+/.test(lines[i]) && !bullet.test(lines[i]) && !numbered.test(lines[i])
    ) {
      para.push(lines[i]);
      i += 1;
    }
    const k = key();
    blocks.push(
      <p key={k}>
        {para.map((row, n) => (
          <React.Fragment key={n}>{n > 0 && <br />}{inline(row, `${k}-${n}`)}</React.Fragment>
        ))}
      </p>,
    );
  }
  return <div className="md">{blocks}</div>;
}
