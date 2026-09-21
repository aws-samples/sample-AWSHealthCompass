import React, { useState } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Button from '@cloudscape-design/components/button';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import { CognitoUser } from 'amazon-cognito-identity-js';
import { useAuth } from './AuthContext';
import { APP_NAME } from './branding';

export default function Login() {
  const { signIn, completeNewPassword } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [challenge, setChallenge] = useState<{ type: string; cognitoUser: CognitoUser } | null>(null);

  const handleSignIn = async () => {
    setError('');
    setLoading(true);
    try {
      const result = await signIn(email, password);
      if (result.challenge === 'NEW_PASSWORD_REQUIRED' && result.cognitoUser) {
        setChallenge({ type: result.challenge, cognitoUser: result.cognitoUser });
      }
    } catch (e: any) {
      const msg = e?.message || 'Authentication failed';
      if (msg.includes('Incorrect username or password')) setError('Incorrect email or password.');
      else if (msg.includes('Password attempts exceeded')) setError('Account temporarily locked. Try again in 15 minutes.');
      else if (msg.includes('User is disabled')) setError('Your account has been disabled. Contact your administrator.');
      else setError(msg);
    } finally {
      setLoading(false);
    }
  };

  const handleNewPassword = async () => {
    setError('');
    if (newPassword !== confirmPassword) { setError('Passwords do not match.'); return; }
    if (newPassword.length < 12) { setError('Password must be at least 12 characters.'); return; }
    setLoading(true);
    try {
      await completeNewPassword(challenge!.cognitoUser, newPassword);
    } catch (e: any) {
      setError(e?.message || 'Failed to set new password.');
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      if (challenge) handleNewPassword();
      else handleSignIn();
    }
  };

  if (challenge) {
    return (
      <Box margin={{ top: 'xxxl' }} padding="xxxl">
        <div style={{ maxWidth: 400, margin: '0 auto' }}>
          <Container header={<Header variant="h2">Set Your Password</Header>}>
            <SpaceBetween size="m">
              {error && <Alert type="error">{error}</Alert>}
              <FormField label="New password">
                <Input type="password" value={newPassword} onChange={e => setNewPassword(e.detail.value)} onKeyDown={handleKeyDown as any} />
              </FormField>
              <FormField label="Confirm password">
                <Input type="password" value={confirmPassword} onChange={e => setConfirmPassword(e.detail.value)} onKeyDown={handleKeyDown as any} />
              </FormField>
              <Box color="text-body-secondary" fontSize="body-s">
                At least 12 characters, 1 uppercase, 1 number, 1 special character.
              </Box>
              <Button variant="primary" fullWidth loading={loading} onClick={handleNewPassword}>
                Set Password &amp; Sign In
              </Button>
            </SpaceBetween>
          </Container>
        </div>
      </Box>
    );
  }

  return (
    <Box margin={{ top: 'xxxl' }} padding="xxxl">
      <div style={{ maxWidth: 400, margin: '0 auto' }}>
        <Container header={<Header variant="h2">Sign in to {APP_NAME}</Header>}>
          <SpaceBetween size="m">
            {error && <Alert type="error">{error}</Alert>}
            <FormField label="Email">
              <Input type="email" value={email} onChange={e => setEmail(e.detail.value)} onKeyDown={handleKeyDown as any} autoFocus />
            </FormField>
            <FormField label="Password">
              <Input type="password" value={password} onChange={e => setPassword(e.detail.value)} onKeyDown={handleKeyDown as any} />
            </FormField>
            <Button variant="primary" fullWidth loading={loading} onClick={handleSignIn}>
              Sign in
            </Button>
          </SpaceBetween>
        </Container>
      </div>
    </Box>
  );
}
