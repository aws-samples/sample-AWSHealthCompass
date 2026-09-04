import React, { useState, useEffect, useCallback } from 'react';
import AppLayout from '@cloudscape-design/components/app-layout';
import SideNavigation from '@cloudscape-design/components/side-navigation';
import TopNavigation from '@cloudscape-design/components/top-navigation';
import Flashbar from '@cloudscape-design/components/flashbar';
import Spinner from '@cloudscape-design/components/spinner';
import Box from '@cloudscape-design/components/box';
import { AuthProvider, useAuth } from './AuthContext';
import { PlatformProvider } from './PlatformContext';
import { resolvePlatformContext } from './platformResolver';
import { apiFetch, isConfigured, setSignOutCallback, setTokenProvider } from './api';
import type { Campaign, OnboardingConfig } from './types';
import Dashboard from './Dashboard';
import Campaigns from './Campaigns';
import Configuration from './Configuration';
import Testing from './Testing';
import RoutingHealth from './RoutingHealth';
import Login from './Login';
import { APP_NAME, APP_PHASE } from './branding';

function AuthenticatedApp() {
  const { user, isAdmin, signOut, getToken } = useAuth();
  const [page, setPage] = useState('dashboard');
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [config, setConfig] = useState<OnboardingConfig | null>(null);
  const [flash, setFlash] = useState<any[]>([]);
  const [syncing, setSyncing] = useState(false);
  const [splitPanel, setSplitPanel] = useState<React.ReactNode>(null);
  const [splitPanelOpen, setSplitPanelOpen] = useState(false);

  // Wire up 401 handler and async token provider
  useEffect(() => {
    setSignOutCallback(signOut);
    setTokenProvider(getToken);
  }, [signOut, getToken]);

  const notify = (type: string, content: string) => {
    const id = String(Date.now());
    setFlash(f => [...f, { type, content, id, dismissible: true, onDismiss: () => setFlash(ff => ff.filter(i => i.id !== id)) }]);
  };

  const loadCampaigns = useCallback(async () => {
    if (!isConfigured()) return;
    try { setCampaigns(await apiFetch('/campaigns')); } catch (e: any) { notify('error', e.message); }
  }, []);

  const loadConfig = useCallback(async () => {
    if (!isConfigured()) return;
    try { setConfig(await apiFetch('/config/summary')); } catch {}
  }, []);

  const syncAll = async () => {
    if (!isConfigured()) return;
    setSyncing(true);
    try {
      await apiFetch('/reconcile', { method: 'POST' });
      await apiFetch('/sync', { method: 'POST' });
      await loadCampaigns();
      notify('success', 'Sync complete');
    } catch (e: any) { notify('error', `Sync failed: ${e.message}`); }
    finally { setSyncing(false); }
  };

  useEffect(() => { loadCampaigns(); loadConfig(); }, []);

  const onSplitPanelChange = (panel: React.ReactNode) => {
    setSplitPanel(panel);
    setSplitPanelOpen(!!panel);
  };

  const activeCampaigns = campaigns.filter(c => c.status === 'active');
  const roleBadge = isAdmin ? 'Admin' : 'Viewer';

  return (
    <PlatformProvider platform={resolvePlatformContext(config).labelPlatform}>
      <TopNavigation
        identity={{ href: '#', title: `${APP_NAME} + ITSM` }}
        utilities={[
          ...(isAdmin ? [{ type: 'button' as const, text: syncing ? 'Syncing...' : 'Sync', onClick: syncAll }] : []),
          { type: 'button' as const, text: `${user?.email || ''} (${roleBadge})`, onClick: () => {} },
          { type: 'button' as const, text: 'Sign out', onClick: signOut },
        ]}
      />
      <AppLayout
        splitPanel={splitPanel}
        splitPanelOpen={splitPanelOpen}
        onSplitPanelToggle={({ detail: d }) => setSplitPanelOpen(d.open)}
        navigation={
          <SideNavigation
            activeHref={`#/${page}`}
            onFollow={e => { e.preventDefault(); const p = e.detail.href.replace('#/', ''); setPage(p); setSplitPanel(null); setSplitPanelOpen(false); }}
            items={[
              { type: 'section', text: 'Health Events', items: [
                { type: 'link', text: 'Dashboard', href: '#/dashboard' },
              ]},
              { type: 'section', text: APP_NAME, items: [
                { type: 'link', text: `Campaigns (${activeCampaigns.length})`, href: '#/campaigns' },
                { type: 'link', text: 'Routing Health', href: '#/routing-health' },
              ]},
              { type: 'section', text: 'Settings', items: [
                { type: 'link', text: 'Configuration', href: '#/configuration' },
              ]},
              ...(isAdmin ? [{ type: 'section' as const, text: `${APP_PHASE} Tools`, items: [
                { type: 'link' as const, text: 'Testing', href: '#/testing' },
              ]}] : []),
            ]}
          />
        }
        content={
          <>
            <Flashbar items={flash} />
            {page === 'dashboard' && <Dashboard campaigns={campaigns} config={config} onRefresh={loadCampaigns} notify={notify} onNavigate={setPage} onSync={syncAll} onSplitPanelChange={onSplitPanelChange} />}
            {page === 'campaigns' && <Campaigns campaigns={activeCampaigns} config={config} onRefresh={loadCampaigns} notify={notify} onSync={syncAll} onSplitPanelChange={onSplitPanelChange} />}
            {page === 'routing-health' && <RoutingHealth />}
            {page === 'configuration' && <Configuration config={config} onSave={() => { loadConfig(); notify('success', 'Configuration saved'); }} />}
            {page === 'testing' && isAdmin && <Testing />}
          </>
        }
        toolsHide
        navigationWidth={220}
      />
    </PlatformProvider>
  );
}

function AppContent() {
  const { isAuthenticated, isLoading } = useAuth();

  if (isLoading) {
    return <Box textAlign="center" margin={{ top: 'xxxl' }}><Spinner size="large" /></Box>;
  }

  if (!isAuthenticated) {
    return <Login />;
  }

  return <AuthenticatedApp />;
}

export default function App() {
  return (
    <AuthProvider>
      <AppContent />
    </AuthProvider>
  );
}
