import test from 'node:test';
import assert from 'node:assert/strict';
import { explainArticles } from '../src/kbExplain.js';

test('per-article leg ranks and shares', () => {
  const ex = explainArticles({ op: 'hybridSearch', filter: { strategy: 'rankFusion' }, result: [
    { article_id: 'KB-009', title: 'Carregamento lento', por_que: { legs: [
      { name: 'vector', rank: 1, contribution: 1 / 61 }, { name: 'lexical', rank: 2, contribution: 1 / 62 }] } }] });
  assert.equal(ex.rows[0].legs[0].name, 'semântica');
  assert.ok(Math.abs(ex.rows[0].legs[0].share + ex.rows[0].legs[1].share - 1) < 1e-9);
});

test('unexpected shapes never throw', () => {
  for (const ev of [null, {}, { op: 'read', result: [] }, { op: 'hybridSearch', result: 'x' },
    { op: 'hybridSearch', result: [null, 3] }]) assert.equal(explainArticles(ev), null);
  const ex = explainArticles({ op: 'hybridSearch', result: [{ article_id: 'a', por_que: 'novo formato', rerank_score: NaN }] });
  assert.equal(ex.rows[0].available, false);
  assert.equal(ex.rows[0].rerank, null);
});
