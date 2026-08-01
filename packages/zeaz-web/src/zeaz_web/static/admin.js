const form = document.getElementById("admin-form");
const keyInput = document.getElementById("admin-key");
const status = document.getElementById("admin-status");
const output = document.getElementById("admin-output");
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const key = keyInput.value;
  status.textContent = "Authenticating…";
  output.textContent = "";
  try {
    const response = await fetch("/api/admin/state", {headers: {"x-zeaz-admin-key": key, accept: "application/json"}, cache: "no-store"});
    keyInput.value = "";
    if (!response.ok) throw new Error("Authentication failed");
    const value = await response.json();
    output.textContent = JSON.stringify(value, null, 2);
    status.textContent = "Authenticated.";
  } catch (error) {
    keyInput.value = "";
    status.textContent = "Authentication failed.";
  }
});
