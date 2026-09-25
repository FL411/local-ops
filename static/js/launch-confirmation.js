'use strict';

export function detectionMatchesCurrentCwd(cwd, detectedCwd, succeeded) {
  const current = typeof cwd === 'string' ? cwd.trim() : '';
  const detected = typeof detectedCwd === 'string' ? detectedCwd.trim() : '';
  if (!current || !detected || succeeded !== true) return false;
  // Project detection and the Windows folder picker can return equivalent
  // paths with different slash direction, case, or a trailing separator.
  // Compare the Windows form so those harmless representations do not force
  // another detection round.
  const normalizeWindowsPath = value => {
    const windowsPath = value.replaceAll('/', '\\');
    // Keep the separator on a drive root: ``D:`` is drive-relative, while
    // ``D:\`` is absolute and they must not unlock the same detection result.
    const driveRoot = /^[a-z]:\\+$/i.test(windowsPath);
    return (driveRoot ? windowsPath.slice(0, 3)
      : windowsPath.replace(/[\\]+$/, '')).toLowerCase();
  };
  return normalizeWindowsPath(current) === normalizeWindowsPath(detected);
}
