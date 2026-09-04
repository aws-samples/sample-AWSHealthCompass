import React from 'react';
import { createRoot } from 'react-dom/client';
import '@cloudscape-design/global-styles/index.css';
import App from './App';
import { loadConfig } from './config';

loadConfig().then(() => {
  createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
});
