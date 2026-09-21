import React, { createContext, useContext } from 'react';
import { getPlatformLabels, type Platform, type PlatformLabels } from './platformLabels';

const PlatformContext = createContext<Platform>('jira');

export function PlatformProvider({ platform, children }: { platform: Platform; children: React.ReactNode }) {
  return <PlatformContext.Provider value={platform}>{children}</PlatformContext.Provider>;
}

export function usePlatformLabels(): PlatformLabels {
  const platform = useContext(PlatformContext);
  return getPlatformLabels(platform);
}
