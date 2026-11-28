import { copyButton } from './ui.js';

const doubleQuoted = /"(?:\\.|[^"\\\r\n])*"/;
const singleQuoted = /'(?:\\.|[^'\\\r\n])*'/;
const templateString = /`(?:\\.|[^`\\\r\n])*`/;
const numbers = /\b(?:0[xX][\da-fA-F]+|0[bB][01]+|0[oO][0-7]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b/;
const grammar = (comments, keywords, strings = [doubleQuoted, singleQuoted]) => ({
  tokens: new RegExp([
    comments && `(?<comment>${comments.source})`,
    `(?<string>${strings.map((pattern) => pattern.source).join('|')})`,
    `(?<number>${numbers.source})`, /[\w$]+/.source
  ].filter(Boolean).join('|'), 'g'),
  keywords: new Set(keywords.trim().split(/\s+/))
});
// shortcut: lexical colors cover common tokens, use a grammar-based parser if multiline syntax needs accurate colors.
const python = grammar(/#[^\r\n]*/, `
  and as assert async await break case class continue def del elif else except False finally for
  from global if import in is lambda match None nonlocal not or pass raise return True try while
  with yield
`);
const javascript = grammar(/\/\/[^\r\n]*|\/\*[\s\S]*?(?:\*\/|$)/, `
  as async await break case catch class const continue debugger declare default delete do else enum
  export extends false finally for from function if implements import in instanceof interface let
  new null of private protected public readonly return static super switch this throw true try type
  typeof undefined var void while with yield
`, [doubleQuoted, singleQuoted, templateString]);
const json = grammar(null, 'true false null', [doubleQuoted]);
const shell = grammar(/#[^\r\n]*/,
  'case do done elif else esac export fi for function if in local readonly return select set then unset until while');
const languages = new Map([
  ['python', python], ['py', python],
  ['javascript', javascript], ['js', javascript], ['jsx', javascript],
  ['typescript', javascript], ['ts', javascript], ['tsx', javascript],
  ['json', json], ['sh', shell], ['bash', shell], ['shell', shell], ['zsh', shell]
]);

function codeBlock(source, language) {
  const block = document.createElement('div');
  block.className = 'code-block';
  const bar = document.createElement('div');
  bar.className = 'code-toolbar';
  bar.append(copyButton(source, 'code'));
  const pre = document.createElement('pre');
  pre.tabIndex = 0;
  const code = document.createElement('code');
  const syntax = languages.get(language);
  let offset = 0;
  if (syntax) {
    for (const { 0: token, index, groups } of source.matchAll(syntax.tokens)) {
      const kind = groups.comment ? 'comment' : groups.string ? 'string'
        : groups.number ? 'number' : syntax.keywords.has(token) ? 'keyword' : '';
      if (!kind) continue;
      if (index > offset) code.append(source.slice(offset, index));
      const span = document.createElement('span');
      span.className = `syntax-${kind}`;
      span.textContent = token;
      code.append(span);
      offset = index + token.length;
    }
  }
  if (offset < source.length) code.append(source.slice(offset));
  pre.append(code);
  block.append(bar, pre);
  return block;
}

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
  const source = String(text);
  let fence = null,
    list = null,
    paragraph = null;
  for (const { 0: raw, index } of source.matchAll(/[^\n]*\n|[^\n]+/g)) {
    const line = raw.replace(/\r?\n$/, '');
    const marker = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (fence) {
      if (marker && marker[1][0] === fence.marker[0] &&
          marker[1].length >= fence.marker.length && !marker[2].trim()) {
        root.append(codeBlock(source.slice(fence.start, index), fence.language));
        fence = null;
      }
      continue;
    }
    if (marker && (marker[1][0] === '~' || !marker[2].includes('`'))) {
      fence = {
        marker: marker[1],
        language: marker[2].trim().split(/\s+/, 1)[0].toLowerCase(),
        start: index + raw.length
      };
      list = paragraph = null;
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
  if (fence) root.append(codeBlock(source.slice(fence.start), fence.language));
  return root;
}
