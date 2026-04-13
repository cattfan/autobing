use std::fs;
use std::path::{Path, PathBuf};

use thiserror::Error;

#[derive(Debug, Error)]
pub enum VaultError {
    #[error("vault is only supported on Windows in this implementation")]
    UnsupportedPlatform,
    #[error("invalid vault key")]
    InvalidKey,
    #[error("secret for key not found: {0}")]
    Missing(String),
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("windows DPAPI operation failed")]
    Dpapi,
}

pub fn workspace_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .expect("workspace root")
        .to_path_buf()
}

pub fn vault_root() -> PathBuf {
    workspace_root().join(".omx").join("vault")
}

pub fn key_to_filename(key: &str) -> Result<String, VaultError> {
    let trimmed = key.trim();
    if trimmed.is_empty() {
        return Err(VaultError::InvalidKey);
    }
    let encoded: String = trimmed.as_bytes().iter().map(|b| format!("{b:02x}")).collect();
    Ok(format!("{encoded}.bin"))
}

pub fn vault_path_for_key(key: &str) -> Result<PathBuf, VaultError> {
    Ok(vault_root().join(key_to_filename(key)?))
}

#[cfg(windows)]
fn protect_bytes(secret: &[u8]) -> Result<Vec<u8>, VaultError> {
    use std::ptr::{null, null_mut};
    use windows_sys::Win32::Security::Cryptography::{
        CryptProtectData, CRYPT_INTEGER_BLOB, CRYPTPROTECT_UI_FORBIDDEN,
    };
    use windows_sys::Win32::Foundation::LocalFree;

    let input = CRYPT_INTEGER_BLOB {
        cbData: secret.len() as u32,
        pbData: secret.as_ptr() as *mut u8,
    };
    let mut output = CRYPT_INTEGER_BLOB {
        cbData: 0,
        pbData: null_mut(),
    };

    let ok = unsafe {
        CryptProtectData(
            &input,
            null_mut(),
            null(),
            null(),
            null_mut(),
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut output,
        )
    };
    if ok == 0 {
        return Err(VaultError::Dpapi);
    }

    let bytes = unsafe { std::slice::from_raw_parts(output.pbData, output.cbData as usize) }.to_vec();
    unsafe {
        LocalFree(output.pbData.cast());
    }
    Ok(bytes)
}

#[cfg(windows)]
fn unprotect_bytes(secret: &[u8]) -> Result<Vec<u8>, VaultError> {
    use std::ptr::{null, null_mut};
    use windows_sys::Win32::Security::Cryptography::{
        CryptUnprotectData, CRYPT_INTEGER_BLOB, CRYPTPROTECT_UI_FORBIDDEN,
    };
    use windows_sys::Win32::Foundation::LocalFree;

    let input = CRYPT_INTEGER_BLOB {
        cbData: secret.len() as u32,
        pbData: secret.as_ptr() as *mut u8,
    };
    let mut output = CRYPT_INTEGER_BLOB {
        cbData: 0,
        pbData: null_mut(),
    };

    let ok = unsafe {
        CryptUnprotectData(
            &input,
            null_mut(),
            null(),
            null(),
            null_mut(),
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut output,
        )
    };
    if ok == 0 {
        return Err(VaultError::Dpapi);
    }

    let bytes = unsafe { std::slice::from_raw_parts(output.pbData, output.cbData as usize) }.to_vec();
    unsafe {
        LocalFree(output.pbData.cast());
    }
    Ok(bytes)
}

#[cfg(not(windows))]
fn protect_bytes(_secret: &[u8]) -> Result<Vec<u8>, VaultError> {
    Err(VaultError::UnsupportedPlatform)
}

#[cfg(not(windows))]
fn unprotect_bytes(_secret: &[u8]) -> Result<Vec<u8>, VaultError> {
    Err(VaultError::UnsupportedPlatform)
}

pub fn store_secret(key: &str, secret: &str) -> Result<PathBuf, VaultError> {
    let path = vault_path_for_key(key)?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let encrypted = protect_bytes(secret.as_bytes())?;
    fs::write(&path, encrypted)?;
    Ok(path)
}

pub fn read_secret(key: &str) -> Result<String, VaultError> {
    let path = vault_path_for_key(key)?;
    if !path.exists() {
        return Err(VaultError::Missing(key.to_string()));
    }
    let encrypted = fs::read(path)?;
    let bytes = unprotect_bytes(&encrypted)?;
    Ok(String::from_utf8_lossy(&bytes).trim().to_string())
}

pub fn materialize_secret_ref(key: &str, job_id: &str) -> Result<String, VaultError> {
    let secret = read_secret(key)?;
    let secret_dir = workspace_root().join(".omx").join("worker-secrets").join(job_id);
    fs::create_dir_all(&secret_dir)?;
    let secret_path = secret_dir.join("secret.txt");
    fs::write(&secret_path, secret)?;
    Ok(format!("file:{}", secret_path.display()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn key_to_filename_is_stable_and_non_empty() {
        let filename = key_to_filename("account/main").expect("filename");
        assert!(filename.ends_with(".bin"));
        assert!(filename.len() > 4);
    }

    #[cfg(windows)]
    #[test]
    fn dpapi_round_trip_restores_original_secret() {
        let key = format!("test-key-{}", std::process::id());
        let secret = "super-secret";
        let path = store_secret(&key, secret).expect("store");
        let round_trip = read_secret(&key).expect("read");
        assert_eq!(round_trip, secret);
        let _ = fs::remove_file(path);
    }
}
