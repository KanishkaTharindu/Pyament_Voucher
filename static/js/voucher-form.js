(() => {
  const form = document.getElementById("voucher-form");
  if (!form) return;

  const approvalRequired = document.getElementById("id_requires_approval");
  const approver = document.getElementById("id_approver");
  const approverContainer = document.querySelector("[data-approver-container]");
  const branch = document.getElementById("id_branch");
  const refreshApprovers = async () => {
    if (!approver || !branch) return;
    const selected = approver.value;
    approver.replaceChildren(new Option("Loading authorized approvers…", ""));
    if (!branch.value) {
      approver.replaceChildren(new Option("Select a branch first", ""));
      return;
    }
    const url = new URL(form.dataset.approverUrl, window.location.origin);
    url.searchParams.set("branch", branch.value);
    try {
      const response = await fetch(url, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error("Unable to load authorized approvers.");
      const data = await response.json();
      approver.replaceChildren(new Option("Select an authorized approver", ""));
      approver.setCustomValidity("");
      data.approvers.forEach((user) => {
        approver.add(new Option(user.label, user.id, false, String(user.id) === selected));
      });
    } catch (error) {
      approver.replaceChildren(new Option("Approvers unavailable; reload or contact an administrator", ""));
      approver.setCustomValidity(error.message);
    }
  };
  const updateApprovalFields = () => {
    const required = approvalRequired?.checked ?? true;
    if (approverContainer) approverContainer.hidden = !required;
    if (approver) approver.required = required;
  };

  approvalRequired?.addEventListener("change", updateApprovalFields);
  branch?.addEventListener("change", refreshApprovers);
  updateApprovalFields();
  if (branch?.value) refreshApprovers();
})();
