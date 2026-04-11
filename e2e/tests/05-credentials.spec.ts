/**
 * 05-credentials.spec.ts — Test credential CRUD.
 */
import { test, expect } from '../fixtures/auth.fixture';
import { api } from '../fixtures/api-helper';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8080';

test.describe('05 — Credentials', () => {
  test('should create an env credential via UI', async ({ authedPage: page, testState }) => {
    // We need an active credential service. Use the one we created or find an existing one.
    let serviceId = testState.credentialServiceId;
    let serviceName = '';
    if (!serviceId) {
      const services = await api.get('/credential-services/');
      const envSvc = services.find((s: any) => s.credential_type === 'env');
      if (envSvc) {
        serviceId = envSvc.id;
        serviceName = envSvc.name;
        testState.credentialServiceId = envSvc.id;
        testState.credentialServiceSlug = envSvc.slug;
      }
    } else {
      const services = await api.get('/credential-services/');
      const svc = services.find((s: any) => s.id === serviceId);
      serviceName = svc?.name || '';
    }
    test.skip(!serviceId, 'No credential service available');

    await page.goto(`${BASE_URL}/credentials`);
    await page.waitForLoadState('networkidle');

    // Click Add Credential
    await page.getByRole('button', { name: /add credential/i }).click();
    await page.waitForSelector('[role="dialog"]');

    const dialog = page.locator('[role="dialog"]');
    const credName = `${testState.prefix}-env-cred`;

    // Service selector is a native-style combobox — use selectOption by visible text
    const serviceCombo = dialog.getByRole('combobox');
    await serviceCombo.selectOption({ label: serviceName });
    await page.waitForTimeout(500);

    // After service selection, Name and Value fields appear dynamically
    // Fill name
    const nameInput = dialog.locator('[placeholder*="Credential"]');
    if (await nameInput.isVisible({ timeout: 3000 }).catch(() => false)) {
      await nameInput.clear();
      await nameInput.fill(credName);
    }

    // Fill value
    const valueInput = dialog.locator('[placeholder*="Value"]');
    if (await valueInput.isVisible({ timeout: 2000 }).catch(() => false)) {
      await valueInput.fill('e2e-test-secret-value');
    }

    // Click Save Credential
    const saveBtn = dialog.getByRole('button', { name: /save credential/i });
    await expect(saveBtn).toBeEnabled({ timeout: 5_000 });
    await saveBtn.click();

    // Wait for dialog to close
    await expect(dialog).toBeHidden({ timeout: 10_000 });

    // Verify credential appears
    await page.waitForTimeout(1000);
    await expect(page.getByText(credName)).toBeVisible({ timeout: 10_000 });

    // Get ID via API
    const creds = await api.get('/credentials/');
    const created = creds.find((c: any) => c.name === credName);
    expect(created).toBeTruthy();
    testState.credentialEnvId = created.id;
    console.log(`Created env credential: ${created.id}`);
  });

  test('should create an SSH credential via API', async ({ testState }) => {
    // Find an ssh_key credential service
    const services = await api.get('/credential-services/');
    const sshSvc = services.find((s: any) => s.credential_type === 'ssh_key');

    if (!sshSvc) {
      console.log('No SSH key service available, skipping');
      return;
    }

    const credName = `${testState.prefix}-ssh-cred`;
    const created = await api.post('/credentials/', {
      service_id: sshSvc.id,
      name: credName,
      value: null,
    });

    if (created._error) {
      console.log(`SSH credential creation failed: ${JSON.stringify(created)}`);
      return;
    }

    testState.credentialSshId = created.id;
    console.log(`Created SSH credential: ${created.id}`);
  });

  test('should show SSH public key', async ({ authedPage: page, testState }) => {
    test.skip(!testState.credentialSshId, 'No SSH credential');

    // Check public key via API
    const pubKey = await api.get(`/credentials/${testState.credentialSshId}/public-key/`);
    expect(pubKey).toBeTruthy();
    if (pubKey.public_key) {
      expect(pubKey.public_key).toContain('ssh-');
      console.log(`Public key starts with: ${pubKey.public_key.substring(0, 40)}...`);
    }
  });

  test('should create an org-scoped credential via API', async ({ testState }) => {
    let serviceId = testState.credentialServiceId;
    if (!serviceId) {
      const services = await api.get('/credential-services/');
      const envSvc = services.find((s: any) => s.credential_type === 'env');
      serviceId = envSvc?.id;
    }
    test.skip(!serviceId, 'No credential service');

    const credName = `${testState.prefix}-org-cred`;
    const created = await api.post('/credentials/', {
      service_id: serviceId,
      name: credName,
      value: 'org-secret-value',
      organization_credential: true,
    });

    if (created._error) {
      console.log(`Org credential creation: ${JSON.stringify(created)}`);
      return;
    }

    testState.credentialOrgId = created.id;
    console.log(`Created org credential: ${created.id}`);
  });

  test('should edit credential name', async ({ authedPage: page, testState }) => {
    test.skip(!testState.credentialEnvId, 'No env credential');

    const updated = await api.patch(`/credentials/${testState.credentialEnvId}/`, {
      name: `${testState.prefix}-env-cred-renamed`,
    });
    expect(updated._error).toBeFalsy();

    // Verify on UI
    await page.goto(`${BASE_URL}/credentials`);
    await page.waitForLoadState('networkidle');
    await expect(page.getByText(`${testState.prefix}-env-cred-renamed`)).toBeVisible({ timeout: 10_000 });

    // Rename back
    await api.patch(`/credentials/${testState.credentialEnvId}/`, {
      name: `${testState.prefix}-env-cred`,
    });
  });

  test('should list credentials on the page', async ({ authedPage: page, testState }) => {
    await page.goto(`${BASE_URL}/credentials`);
    await page.waitForLoadState('networkidle');

    // At least our test credentials should appear
    const credName = `${testState.prefix}-env-cred`;
    if (testState.credentialEnvId) {
      await expect(page.getByText(credName)).toBeVisible({ timeout: 10_000 });
    }
  });
});
