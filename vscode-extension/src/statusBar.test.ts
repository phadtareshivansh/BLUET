import { StatusBarManager } from './statusBar';
import { LanguageClient } from 'vscode-languageclient/node';

// Mock LanguageClient
const mockClient = {} as LanguageClient;

describe('StatusBarManager', () => {
  let statusBarManager: StatusBarManager;

  beforeEach(() => {
    statusBarManager = new StatusBarManager(mockClient);
  });

  afterEach(() => {
    statusBarManager.dispose();
  });

  test('should initialize with idle status', () => {
    expect(statusBarManager.getStatus()).toBe('idle');
  });

  test('should update status to analyzing', () => {
    statusBarManager.updateStatus('analyzing', 'BLUET: Analyzing...');
    expect(statusBarManager.getStatus()).toBe('analyzing');
  });

  test('should update status to pass', () => {
    statusBarManager.updateStatus('pass', 'BLUET: Parity OK');
    expect(statusBarManager.getStatus()).toBe('pass');
  });

  test('should update status to fail', () => {
    statusBarManager.updateStatus('fail', 'BLUET: Parity failed');
    expect(statusBarManager.getStatus()).toBe('fail');
  });

  test('should update status to error', () => {
    statusBarManager.updateStatus('error', 'BLUET: Error');
    expect(statusBarManager.getStatus()).toBe('error');
  });
});