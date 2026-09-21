import React, { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react';
import {
  CognitoUserPool,
  CognitoUser,
  AuthenticationDetails,
  CognitoUserSession,
} from 'amazon-cognito-identity-js';
import { getConfig } from './config';

// Lazy singleton — only created after loadConfig() has completed (called from main.tsx)
let _userPool: CognitoUserPool | null = null;
function getUserPool(): CognitoUserPool {
  if (!_userPool) {
    const { userPoolId, clientId } = getConfig();
    _userPool = new CognitoUserPool({ UserPoolId: userPoolId, ClientId: clientId });
  }
  return _userPool;
}

interface User {
  email: string;
  groups: string[];
}

interface AuthContextValue {
  user: User | null;
  isAuthenticated: boolean;
  isAdmin: boolean;
  isLoading: boolean;
  signIn: (email: string, password: string) => Promise<{ challenge?: string; cognitoUser?: CognitoUser }>;
  completeNewPassword: (cognitoUser: CognitoUser, newPassword: string) => Promise<void>;
  signOut: () => void;
  getToken: () => Promise<string | null>;
}

const AuthContext = createContext<AuthContextValue>({
  user: null,
  isAuthenticated: false,
  isAdmin: false,
  isLoading: true,
  signIn: async () => ({}),
  completeNewPassword: async () => {},
  signOut: () => {},
  getToken: async () => null,
});

export const useAuth = () => useContext(AuthContext);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const refreshTimer = useRef<number | null>(null);

  const extractUser = (session: CognitoUserSession): User => {
    const idToken = session.getIdToken();
    const payload = idToken.decodePayload();
    return {
      email: payload['email'] || payload['sub'] || '',
      groups: payload['cognito:groups'] || [],
    };
  };

  // signOut clears SDK storage and local state — no sessionStorage needed
  const signOut = useCallback(() => {
    const currentUser = getUserPool().getCurrentUser();
    if (currentUser) currentUser.signOut(); // Clears SDK localStorage entries
    setUser(null);
    if (refreshTimer.current) {
      clearTimeout(refreshTimer.current);
      refreshTimer.current = null;
    }
  }, []);

  // Proactive refresh timer — keeps token warm in the background.
  // Uses a 5-minute buffer before expiry to allow for network delays.
  const scheduleRefresh = useCallback((session: CognitoUserSession) => {
    if (refreshTimer.current) {
      clearTimeout(refreshTimer.current);
      refreshTimer.current = null;
    }

    const exp = session.getIdToken().getExpiration() * 1000;
    const now = Date.now();
    const buffer = 5 * 60 * 1000; // 5 minutes before expiry
    const refreshAt = exp - now - buffer;

    const doRefresh = () => {
      const currentUser = getUserPool().getCurrentUser();
      if (!currentUser) {
        signOut();
        return;
      }
      currentUser.getSession((err: Error | null, sess: CognitoUserSession | null) => {
        if (!err && sess && sess.isValid()) {
          setUser(extractUser(sess));
          scheduleRefresh(sess);
        } else {
          // Retry once after 5 seconds before giving up
          refreshTimer.current = window.setTimeout(() => {
            currentUser.getSession((retryErr: Error | null, retrySess: CognitoUserSession | null) => {
              if (!retryErr && retrySess && retrySess.isValid()) {
                setUser(extractUser(retrySess));
                scheduleRefresh(retrySess);
              } else {
                signOut();
              }
            });
          }, 5000);
        }
      });
    };

    if (refreshAt <= 0) {
      // Token is already near expiry or expired — refresh immediately
      doRefresh();
    } else {
      refreshTimer.current = window.setTimeout(doRefresh, refreshAt);
    }
  }, [signOut]);

  // Check for existing session on mount
  useEffect(() => {
    const currentUser = getUserPool().getCurrentUser();
    if (currentUser) {
      currentUser.getSession((err: Error | null, session: CognitoUserSession | null) => {
        if (!err && session && session.isValid()) {
          setUser(extractUser(session));
          scheduleRefresh(session);
        }
        setIsLoading(false);
      });
    } else {
      setIsLoading(false);
    }
    return () => { if (refreshTimer.current) clearTimeout(refreshTimer.current); };
  }, [scheduleRefresh]);

  const signIn = useCallback(async (email: string, password: string): Promise<{ challenge?: string; cognitoUser?: CognitoUser }> => {
    const cognitoUser = new CognitoUser({ Username: email, Pool: getUserPool() });
    const authDetails = new AuthenticationDetails({ Username: email, Password: password });

    return new Promise((resolve, reject) => {
      cognitoUser.authenticateUser(authDetails, {
        onSuccess: (session) => {
          setUser(extractUser(session));
          scheduleRefresh(session);
          resolve({});
        },
        onFailure: (err) => {
          reject(err);
        },
        newPasswordRequired: () => {
          resolve({ challenge: 'NEW_PASSWORD_REQUIRED', cognitoUser });
        },
      });
    });
  }, [scheduleRefresh]);

  const completeNewPassword = useCallback(async (cognitoUser: CognitoUser, newPassword: string): Promise<void> => {
    return new Promise((resolve, reject) => {
      cognitoUser.completeNewPasswordChallenge(newPassword, {}, {
        onSuccess: (session) => {
          setUser(extractUser(session));
          scheduleRefresh(session);
          resolve();
        },
        onFailure: (err) => reject(err),
      });
    });
  }, [scheduleRefresh]);

  // Async getToken — delegates to SDK's getSession() which auto-refreshes if needed.
  // This is the single source of truth for obtaining a valid token.
  const getToken = useCallback(async (): Promise<string | null> => {
    const currentUser = getUserPool().getCurrentUser();
    if (!currentUser) return null;

    return new Promise((resolve) => {
      currentUser.getSession((err: Error | null, session: CognitoUserSession | null) => {
        if (err || !session || !session.isValid()) {
          resolve(null);
        } else {
          resolve(session.getIdToken().getJwtToken());
        }
      });
    });
  }, []);

  const isAuthenticated = !!user;
  const isAdmin = user?.groups?.includes('Admins') ?? false;

  return (
    <AuthContext.Provider value={{ user, isAuthenticated, isAdmin, isLoading, signIn, completeNewPassword, signOut, getToken }}>
      {children}
    </AuthContext.Provider>
  );
}
