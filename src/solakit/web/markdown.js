// A deliberately small Markdown subset. All model text enters through text nodes.
export function markdown(text) {
  const root = document.createDocumentFragment();
  const inline = (parent, source) => {
    const tokens = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|\[[^\]\n]+\]\([^\s)]+\))/g;
    let offset = 0;
    for (const match of source.matchAll(tokens)) {
      parent.append(document.createTextNode(source.slice(offset, match.index)));
      const token = match[0];
      let node;
      if (token.startsWith('[')) {
        const [, label, target] = token.match(/^\[([^\]]+)\]\((.+)\)$/);
        let safe = false;
        try {
          safe = ['https:', 'http:', 'mailto:'].includes(new URL(target).protocol);
        } catch {
          /* plain text */
        }
        node = document.createElement(safe ? 'a' : 'span');
        node.textContent = label;
        if (safe) {
          node.href = target;
          node.rel = 'noopener noreferrer';
          node.target = '_blank';
        }
      } else {
        const size = token.startsWith('**') ? 2 : 1;
        node = document.createElement(token[0] === '`' ? 'code' : size === 2 ? 'strong' : 'em');
        node.textContent = token.slice(size, -size);
      }
      parent.append(node);
      offset = match.index + token.length;
    }
    parent.append(document.createTextNode(source.slice(offset)));
  };
  let code = null,
    list = null,
    paragraph = null;
  for (const line of String(text).split('\n')) {
    if (line.startsWith('```')) {
      if (code) code = null;
      else {
        const pre = document.createElement('pre');
        pre.tabIndex = 0;
        code = document.createElement('code');
        pre.append(code);
        root.append(pre);
      }
      list = paragraph = null;
      continue;
    }
    if (code) {
      code.append(document.createTextNode(`${line}\n`));
      continue;
    }
    if (!line.trim()) {
      list = paragraph = null;
      continue;
    }
    const bullet = line.match(/^\s*(?:[-*+] |\d+\. )(.*)$/);
    if (bullet) {
      const tag = /^\s*\d/.test(line) ? 'ol' : 'ul';
      if (!list || list.localName !== tag) {
        list = document.createElement(tag);
        root.append(list);
      }
      const item = document.createElement('li');
      inline(item, bullet[1]);
      list.append(item);
      paragraph = null;
      continue;
    }
    list = null;
    const heading = line.match(/^(#{1,3}) (.*)$/),
      quote = line.match(/^> ?(.*)$/);
    if (heading || quote) {
      const node = document.createElement(heading ? `h${heading[1].length}` : 'blockquote');
      inline(node, heading ? heading[2] : quote[1]);
      root.append(node);
      paragraph = null;
    } else {
      if (!paragraph) {
        paragraph = document.createElement('p');
        root.append(paragraph);
      } else paragraph.append(document.createElement('br'));
      inline(paragraph, line);
    }
  }
  return root;
}
