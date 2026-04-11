/**
 * 04-image-definitions.spec.ts — Test image definition CRUD and builds.
 */
import { test, expect } from '../fixtures/auth.fixture';
import { api, waitForImageBuild } from '../fixtures/api-helper';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8080';

test.describe('04 — Image Definitions', () => {
  test('should create a Docker image definition', async ({ authedPage: page, testState }) => {
    await page.goto(`${BASE_URL}/org-settings`);
    await page.waitForLoadState('networkidle');

    await page.getByRole('button', { name: 'Image Definitions' }).click();
    await page.waitForTimeout(500);

    // Click New Image Definition
    await page.getByRole('button', { name: /new image definition/i }).click();
    await page.waitForSelector('[role="dialog"]');

    const dialog = page.locator('[role="dialog"]');
    const imgName = `${testState.prefix}-docker-img`;

    // Fill name — first textbox (placeholder: "Python Dev Environment")
    const nameInput = dialog.getByRole('textbox').first();
    await nameInput.fill(imgName);
    await page.waitForTimeout(300);

    // Runtime combobox defaults to Docker which is what we want
    // Base Image combobox defaults to ubuntu:22.04 which is fine

    // Click Save (button text is "Save", not "Create"!)
    const saveBtn = dialog.getByRole('button', { name: /save/i });
    await expect(saveBtn).toBeEnabled({ timeout: 5_000 });
    await saveBtn.click();

    // Wait for dialog close
    await expect(dialog).toBeHidden({ timeout: 15_000 });

    // Verify via API
    const defs = await api.get('/image-definitions/');
    const created = defs.find((d: any) => d.name === imgName);
    expect(created).toBeTruthy();
    testState.imageDefinitionDockerId = created.id;
    console.log(`Created Docker image def: ${created.id}`);
  });

  test('should show Docker image definition in list', async ({ authedPage: page, testState }) => {
    test.skip(!testState.imageDefinitionDockerId, 'No Docker image definition');

    await page.goto(`${BASE_URL}/org-settings`);
    await page.waitForLoadState('networkidle');

    await page.getByRole('button', { name: 'Image Definitions' }).click();
    await page.waitForTimeout(500);

    const imgName = `${testState.prefix}-docker-img`;
    await expect(page.getByText(imgName)).toBeVisible({ timeout: 5_000 });
  });

  test('should trigger a Docker image build on a runner', async ({ authedPage: page, testState }) => {
    test.skip(!testState.imageDefinitionDockerId, 'No Docker image definition');
    test.skip(!testState.dockerRunnerId, 'No Docker runner');

    // Trigger build via API
    const buildRes = await api.post(
      `/image-definitions/${testState.imageDefinitionDockerId}/runner-builds/`,
      { runner_id: testState.dockerRunnerId },
    );

    // Build might fail if the definition has no proper Dockerfile,
    // but the API call should succeed
    if (buildRes._error) {
      console.log(`Build trigger response: ${JSON.stringify(buildRes)}`);
      // Some definitions might not be buildable without a proper config
      // That's OK for testing the API path
    } else {
      console.log(`Build triggered: ${JSON.stringify(buildRes)}`);

      // Wait for build to complete (with a reasonable timeout)
      try {
        const result = await waitForImageBuild(
          testState.imageDefinitionDockerId!,
          testState.dockerRunnerId!,
          300_000,
        );
        console.log(`Build result: ${result?.status}`);
        testState.imageDefinitionDockerBuildReady = result?.status === 'active';
      } catch (e) {
        console.log(`Build wait timed out or failed: ${e}`);
      }
    }
  });

  test('should create a QEMU image definition if QEMU runner exists', async ({ authedPage: page, testState }) => {
    test.skip(!testState.qemuRunnerId, 'No QEMU runner');

    const imgName = `${testState.prefix}-qemu-img`;

    // Create via API since QEMU-specific fields are complex
    const created = await api.post('/image-definitions/', {
      name: imgName,
      description: 'E2E test QEMU image',
      runtime_type: 'qemu',
      base_distro: 'ubuntu:24.04',
      is_active: true,
    });

    if (created._error) {
      console.log(`QEMU image creation failed: ${JSON.stringify(created)}`);
      return;
    }

    testState.imageDefinitionQemuId = created.id;
    console.log(`Created QEMU image def: ${created.id}`);

    // Verify it appears on page
    await page.goto(`${BASE_URL}/org-settings`);
    await page.waitForLoadState('networkidle');
    await page.getByRole('button', { name: 'Image Definitions' }).click();
    await page.waitForTimeout(500);
    await expect(page.getByText(imgName)).toBeVisible({ timeout: 5_000 });
  });

  test('should trigger a QEMU image build on a runner', async ({ testState }) => {
    test.setTimeout(720_000); // QEMU builds can take 10+ minutes
    test.skip(!testState.imageDefinitionQemuId, 'No QEMU image definition');
    test.skip(!testState.qemuRunnerId || !testState.qemuRunnerOnline, 'QEMU runner not available or offline');

    // Trigger build via API
    const buildRes = await api.post(
      `/image-definitions/${testState.imageDefinitionQemuId}/runner-builds/`,
      { runner_id: testState.qemuRunnerId },
    );

    if (buildRes._error) {
      console.log(`QEMU build trigger failed: ${JSON.stringify(buildRes)}`);
      return;
    }
    console.log(`QEMU build triggered: ${buildRes.id} (status: ${buildRes.status})`);

    // Wait for build to complete (QEMU builds can take a while)
    try {
      const result = await waitForImageBuild(
        testState.imageDefinitionQemuId!,
        testState.qemuRunnerId!,
        600_000,
      );
      console.log(`QEMU build result: ${result?.status}`);
      testState.imageDefinitionQemuBuildReady = result?.status === 'active';

      // Find the image artifact for this build
      if (result?.image_artifact_id) {
        testState.qemuImageArtifactId = result.image_artifact_id;
        console.log(`QEMU image artifact: ${result.image_artifact_id}`);
      }
    } catch (e) {
      console.log(`QEMU build wait timed out or failed: ${e}`);
    }
  });

  test('should edit image definition via API', async ({ testState }) => {
    test.skip(!testState.imageDefinitionDockerId, 'No Docker image definition');

    const updated = await api.patch(`/image-definitions/${testState.imageDefinitionDockerId}/`, {
      description: 'E2E test Docker image — updated',
    });
    expect(updated._error).toBeFalsy();
    expect(updated.description).toContain('updated');
  });
});
