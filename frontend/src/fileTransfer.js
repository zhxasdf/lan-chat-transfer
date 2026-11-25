export const MAX_FILE_SIZE = 20 * 1024 * 1024;

export function formatSize(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KiB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MiB`;
}

export function uploadFile(file, token, target, progress) {
  const xhr = new XMLHttpRequest();
  const query = new URLSearchParams({ filename: file.name });
  if (target) query.set("to", target.id);
  const done = new Promise((resolve, reject) => {
    xhr.open("POST", `/api/files?${query}`);
    xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.timeout = 120000;
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable)
        progress(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onload = () => {
      let result;
      try {
        result = JSON.parse(xhr.responseText);
      } catch {
        result = {};
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(result);
      else
        reject(
          new Error(
            typeof result.detail === "string"
              ? result.detail
              : `上传失败（${xhr.status}）`,
          ),
        );
    };
    xhr.onerror = () =>
      reject(new Error("上传连接失败，请检查网络后重新选择文件"));
    xhr.ontimeout = () => reject(new Error("上传超时，请重新选择文件"));
    xhr.onabort = () => reject(new Error("上传已取消"));
    xhr.send(file);
  });
  return { done, cancel: () => xhr.abort() };
}

export async function downloadFile(file, token, signal) {
  const response = await fetch(`/api/files/${encodeURIComponent(file.id)}`, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
    cache: "no-store",
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : `下载失败（${response.status}）`,
    );
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = file.name;
  document.body.append(link);
  link.click();
  link.remove();
  // Give the browser time to consume the URL before releasing its backing blob.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
