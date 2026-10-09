import test from 'node:test';
import assert from 'node:assert/strict';
import { blocks, inline } from '../src/richtext.js';

test('bold markers become bold segments, never literal asterisks', () => {
  assert.deepEqual(inline('status **processando** hoje'), [
    { text: 'status ' }, { text: 'processando', bold: true }, { text: ' hoje' }]);
});
test('bullet lines are grouped into one list; headings lose the hashes', () => {
  const out = blocks('## Opções\nPosso ajudar com:\n- qual é o status;\n- a **fatura**\n\nFim');
  assert.equal(out[0].type, 'p'); assert.equal(out[0].segments[0].text, 'Opções');
  assert.equal(out[2].type, 'list'); assert.equal(out[2].items.length, 2);
  assert.deepEqual(out[2].items[1], [{ text: 'a ' }, { text: 'fatura', bold: true }]);
  assert.equal(out.at(-1).segments[0].text, 'Fim');
});
test('html stays plain text (React escapes it)', () => {
  assert.deepEqual(blocks('<b>x</b>'), [{ type: 'p', segments: [{ text: '<b>x</b>' }] }]);
});
