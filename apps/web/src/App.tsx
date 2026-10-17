import { useEffect, useState } from 'react';
import { Route, Routes } from 'react-router-dom';
import Home from './HomePages';
import Workspace from './Workspace';

function UpdateNotice() {
  const [available, setAvailable] = useState(false);
  useEffect(() => {
    const loadedBundle = document.querySelector<HTMLScriptElement>('script[src*="/assets/index-"]')?.getAttribute('src');
    if (!loadedBundle) return;
    const check = async () => {
      try {
        const response = await fetch('/', {cache: 'no-store'});
        if (!response.ok) return;
        const latestBundle = (await response.text()).match(/\/assets\/index-[A-Za-z0-9_-]+\.js/)?.[0];
        if (latestBundle && latestBundle !== loadedBundle) setAvailable(true);
      } catch { /* A temporary network error should not interrupt the workspace. */ }
    };
    const onFocus = () => {void check();};
    const onVisibility = () => {if (document.visibilityState === 'visible') void check();};
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onVisibility);
    const timer = window.setInterval(onFocus, 60_000);
    return () => {window.removeEventListener('focus', onFocus); document.removeEventListener('visibilitychange', onVisibility); window.clearInterval(timer);};
  }, []);
  return available ? <div className="app-update-notice" role="status"><span>Atlas was updated. Reload to use the latest version.</span><button className="button primary" onClick={() => window.location.reload()}>Reload</button></div> : null;
}

export default function App() {
  return <><Routes>
    <Route path="/" element={<Home/>}/>
    <Route path="/welcome" element={<Home forceLanding/>}/>
    <Route path="/analysis/:id" element={<Workspace/>}/>
    <Route path="*" element={<Home/>}/>
  </Routes><UpdateNotice/></>;
}
