// Cabeçalho único das abas: a coleção que sustenta a tela, um título curto e uma frase.
export default function PageHead({ source, title, children, actions }) {
  return (
    <header className="page-head">
      <div className="page-head-copy">
        {source && <code className="page-head-source">{source}</code>}
        <h2>{title}</h2>
        {children && <p>{children}</p>}
      </div>
      {actions && <div className="page-head-actions">{actions}</div>}
    </header>
  );
}
