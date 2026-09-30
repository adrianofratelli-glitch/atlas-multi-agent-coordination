/**
 * Cadeia de trocas — a leitura visual do que o $graphLookup devolveu.
 *
 * O evento de timeline com op="graphLookup" carrega o resumo (path, replacements,
 * recurring_defect). Aqui ele vira a corrente de pedidos, que é a única forma de mostrar
 * numa tela por que a resposta mudou: nenhum documento sozinho diz "é a quarta vez".
 */
export default function ReplacementChain({ timeline }) {
  const event = (timeline || []).find((item) => item.op === 'graphLookup');
  const chain = event?.result;
  if (!chain?.path?.length) return null;

  const recurring = Boolean(chain.recurring_defect);
  const reasons = chain.reasons || [];

  return (
    <section className={`chain-panel ${recurring ? 'chain-alert' : ''}`} aria-label="Cadeia de trocas do pedido">
      <div className="panel-label">
        <span>cadeia de trocas · travessia de grafo</span>
        <code>$graphLookup</code>
      </div>

      <ol className="chain-track">
        {chain.path.map((orderId, index) => (
          <li className="chain-node" key={orderId || index}>
            {index > 0 && (
              <span className="chain-link" aria-hidden="true" title={reasons[index - 1] || 'reposição'}>
                <i />repôs<i />
              </span>
            )}
            <span className={`chain-pill ${index === 0 ? 'origin' : ''} ${index === chain.path.length - 1 ? 'current' : ''}`}>
              <code>{orderId}</code>
              <small>{index === 0 ? 'pedido original' : `${index}ª reposição`}</small>
            </span>
          </li>
        ))}
      </ol>

      <div className="chain-signals">
        <div className="chain-signal">
          <strong>{chain.replacements}</strong>
          <span>reposições na cadeia</span>
        </div>
        <div className="chain-signal">
          <strong>{chain.same_product_count ?? chain.same_sku_count ?? '—'}</strong>
          <span>unidades do mesmo produto</span>
        </div>
        <div className="chain-signal">
          <strong>{chain.distinct_products ?? 1}</strong>
          <span>produtos distintos</span>
        </div>
      </div>

      {/* Cor nunca é o único indicador: ícone + rótulo acompanham o estado. */}
      <p className={`chain-verdict ${recurring ? 'alert' : 'ok'}`}>
        <span aria-hidden="true">{recurring ? '⚠' : '✓'}</span>
        {recurring
          ? `Defeito recorrente: ${chain.product || 'o mesmo item'} falhou repetidas vezes. Trocar de novo tende a repetir o problema — o caso vai para análise humana de qualidade.`
          : 'Cadeia curta: reposição pontual, sem padrão de defeito de lote. Atendimento segue o fluxo normal.'}
      </p>

      <p className="chain-footnote">
        Uma agregação percorreu a cadeia inteira dentro do banco. Sem <code>$graphLookup</code> seriam{' '}
        {chain.replacements + 1} idas ao MongoDB — e o número de saltos não é conhecido antes de percorrer.
      </p>
    </section>
  );
}
