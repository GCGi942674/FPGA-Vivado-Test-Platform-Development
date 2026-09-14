export interface Token {
  text: string;
  cls: string;
}

const KW_CPP = new Set([
  'int','void','return','if','else','for','while','do','const','static',
  'unsigned','char','long','double','float','struct','class','bool',
  'nullptr','true','false','auto','new','delete','public','private',
  'protected','this','virtual','override','inline','template','typename',
  'namespace','using','typedef','switch','case','break','continue',
  'default','sizeof','explicit','mutable','operator','friend','extern',
  'volatile','signed','enum','union','constexpr','static_cast','const_cast',
  'reinterpret_cast','dynamic_cast',
]);

const KW_IDA = new Set([
  '__int64','__int32','__int16','__int8','_BOOL8','_BOOL4',
  '__fastcall','__cdecl','__stdcall','__thiscall',
  '_QWORD','_DWORD','_WORD','_BYTE','BOOL','LOBYTE','HIWORD','LOWORD',
]);

export function tokenizeLine(line: string): Token[] {
  const tokens: Token[] = [];
  // Pure comment line
  if (/^\s*\/\//.test(line)) {
    return [{ text: line, cls: 'text-tn-dim italic' }];
  }

  let i = 0;
  while (i < line.length) {
    // Whitespace
    if (/\s/.test(line[i])) {
      let j = i;
      while (j < line.length && /\s/.test(line[j])) j++;
      tokens.push({ text: line.slice(i, j), cls: '' });
      i = j;
      continue;
    }
    // Inline comment
    if (line[i] === '/' && line[i + 1] === '/') {
      tokens.push({ text: line.slice(i), cls: 'text-tn-dim italic' });
      break;
    }
    // String
    if (line[i] === '"') {
      let j = i + 1;
      while (j < line.length && !(line[j] === '"' && line[j-1] !== '\\')) j++;
      tokens.push({ text: line.slice(i, j + 1), cls: 'text-tn-green' });
      i = j + 1;
      continue;
    }
    // Hex number
    const hex = line.slice(i).match(/^0x[0-9a-fA-F]+/);
    if (hex) {
      tokens.push({ text: hex[0], cls: 'text-tn-orange' });
      i += hex[0].length;
      continue;
    }
    // Decimal / float number
    const num = line.slice(i).match(/^-?\d+(?:\.\d+)?(?:[uUlLfF]+)?(?!\w)/);
    if (num && (i === 0 || !/\w/.test(line[i-1]))) {
      tokens.push({ text: num[0], cls: 'text-tn-orange' });
      i += num[0].length;
      continue;
    }
    // Identifier or keyword
    if (/[a-zA-Z_]/.test(line[i])) {
      let j = i;
      while (j < line.length && /\w/.test(line[j])) j++;
      const word = line.slice(i, j);
      if (KW_IDA.has(word)) {
        tokens.push({ text: word, cls: 'text-tn-cyan' });
      } else if (KW_CPP.has(word)) {
        tokens.push({ text: word, cls: 'text-tn-purple' });
      } else if (/^[A-Z]/.test(word)) {
        tokens.push({ text: word, cls: 'text-tn-cyan' });
      } else {
        tokens.push({ text: word, cls: 'text-tn-text' });
      }
      i = j;
      continue;
    }
    // Everything else: operators, punctuation
    tokens.push({ text: line[i], cls: 'text-tn-dim2' });
    i++;
  }
  return tokens;
}
