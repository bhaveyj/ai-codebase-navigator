import { useState } from 'react';
import { ArrowRight, Code2, FileCode2, Network, ShieldCheck, Sparkles } from 'lucide-react';
import { StatusDot } from './RepositoryPanels';
export default function ProductPreview() {
  const [step, setStep] = useState<'map'|'answer'|'source'>('map');
  return <section className="product-preview" aria-label="Beacon Store example walkthrough">
    <header className="preview-windowbar"><div className="preview-window-title"><span className="preview-window-mark"/><span>BEACON STORE</span><span className="preview-window-divider">/</span><strong>src/server</strong></div><span className="preview-ready"><StatusDot ready/> MAP READY</span></header>
    <div className="preview-topline"><span>EXPLORE THE EXAMPLE</span><span>{step === 'map' ? '01' : step === 'answer' ? '02' : '03'} / 03</span></div>
    <div className="preview-tabs" aria-label="Example views">
      <button className={step === 'map' ? 'active' : ''} aria-pressed={step === 'map'} onClick={() => setStep('map')}><Network size={15}/> Map</button>
      <button className={step === 'answer' ? 'active' : ''} aria-pressed={step === 'answer'} onClick={() => setStep('answer')}><Sparkles size={15}/> Answer</button>
      <button className={step === 'source' ? 'active' : ''} aria-pressed={step === 'source'} onClick={() => setStep('source')}><Code2 size={15}/> Source</button>
    </div>
    <div className={`preview-stage preview-stage-${step}`}>
      {step === 'map' && <div className="preview-map">
        <div className="preview-map-caption"><span>DEPENDENCY MAP</span><strong>Follow the path</strong></div>
        <svg className="preview-map-lines" viewBox="0 0 560 320" preserveAspectRatio="none" aria-hidden="true"><path d="M 245 170 H 283 V 88 H 318"/><path d="M 245 170 H 283 V 236 H 318"/><circle cx="283" cy="170" r="3"/><circle cx="318" cy="88" r="3"/><circle cx="318" cy="236" r="3"/></svg>
        <div className="preview-node preview-node-main"><span><FileCode2 size={13}/> ENTRY FILE</span><strong>app.ts</strong><small>src/server</small></div>
        <div className="preview-node preview-node-limit"><span><FileCode2 size={13}/> MIDDLEWARE</span><strong>rate-limit.ts</strong><small>protects API routes</small></div>
        <div className="preview-node preview-node-auth"><span><FileCode2 size={13}/> ROUTE</span><strong>auth.ts</strong><small>authentication</small></div>
        <div className="preview-map-note"><span className="preview-pulse"/> Follow a connection into its source</div>
      </div>}
      {step === 'answer' && <div className="preview-answer"><span className="preview-label"><Sparkles size={15}/> ATLAS ANSWER</span><h3>Where is rate limiting implemented?</h3><p>The server registers the rate limit middleware before its API routes. You can verify the relationship in the source.</p><button className="preview-citation" onClick={() => setStep('source')}><FileCode2 size={16}/>src/server/app.ts:8<ArrowRight size={15}/></button><div className="preview-answer-foot"><ShieldCheck size={14}/> Linked to the analyzed commit</div></div>}
      {step === 'source' && <div className="preview-source"><div className="preview-source-header"><FileCode2 size={15}/><span>src/server/app.ts</span><span>READ ONLY</span></div><pre><span><i>5</i> </span>{'\n'}<span><i>6</i> export const app = express();</span>{'\n'}<span><i>7</i> app.use(express.json());</span>{'\n'}<mark><i>8</i> app.use(rateLimit);</mark>{'\n'}<span><i>9</i> app.use('/api/auth', authRouter);</span>{'\n'}<span><i>10</i> app.use('/api/checkout', checkoutRouter);</span></pre><div className="preview-source-foot"><span>L8</span> Evidence in context</div></div>}
    </div>
    <footer className="preview-footer"><span>MODULES → FILES → SOURCE</span><span>BEACON STORE / EXAMPLE</span></footer>
  </section>;
}



