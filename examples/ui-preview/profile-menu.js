// The table and More actions share one nonmodal, keyboard-operable menu.
// Each action is enabled only when it can work; a multi-row selection
// offers only deleting that selection.
let profileMenuAnchor;
function closeProfileMenu(restore = true) {
  const menu = document.getElementById("profileActions");
  if (!menu) return;
  menu.remove();
  profileMenuAnchor?.setAttribute("aria-expanded", "false");
  if (restore) profileMenuAnchor?.focus({ preventScroll: true });
}
// [label, run, disabledReason] for one profile, in the real menu's order.
function profileMenuActions(id) {
  const m = profileMeta[id],
    busy = profilesBusy() ? "Finish the current PenguinBurner action first." : "";
  const unverified = m.valid ? "" : "Verify this profile first.";
  return [
    ["Edit V/F curve", () => openEditor("vf", id), ""],
    ["Edit fan curve", () => openEditor("fan", id), hasFanCurve(id) ? "" : "No editable fan curve is available for this profile."],
    [
      "Edit memory offset",
      () => memoryDialog(id),
      !canEditMemory(id)
        ? "No editable memory offset is available for this profile."
        : targetGpu() === null
          ? "Choose this profile's GPU first."
          : "",
    ],
    ["Apply", () => applyProfile(id), busy || unverified || (profileMatchesTarget(id) ? "" : applyRefusal(id))],
    ["Verify…", () => verifyProfile(id), busy],
    ["Export LACT…", () => exportProfile(id), busy || unverified],
    ["Assign tier", "tiers", busy || unverified],
    ["Delete", () => deleteSelected([id]), busy],
  ];
}
function openProfileMenu(event, id) {
  event.preventDefault();
  const more = event.currentTarget?.id === "moreProfileActions";
  const previous = document.getElementById("profileActions");
  if (previous) {
    closeProfileMenu(false);
    if (more) return;
  }
  // Right-clicking inside a selection keeps it; outside, it selects the row.
  if (!profileSel.has(id)) chooseProfile(id);
  const selection = selectedIds();
  profileMenuAnchor = more
    ? document.getElementById("moreProfileActions")
    : document.querySelector('[data-profile="' + id + '"]');
  if (!profileMenuAnchor) return;
  profileMenuAnchor.setAttribute("aria-expanded", "true");
  const rect = profileMenuAnchor.getBoundingClientRect();
  const pointer = event.type === "contextmenu" && (event.clientX || event.clientY);
  const menu = document.createElement("div");
  menu.id = "profileActions";
  menu.className = "contextMenu";
  menu.setAttribute("role", "menu");
  menu.setAttribute(
    "aria-label",
    selection.length > 1 ? selection.length + " selected profiles" : "Actions for " + profiles[id][0],
  );
  document.body.append(menu);
  const actions =
    selection.length > 1
      ? [
          [
            "Delete " + selection.length + " selected profiles",
            () => deleteSelected(selection),
            profilesBusy() ? "Finish the current PenguinBurner action first." : "",
          ],
        ]
      : profileMenuActions(id);
  function fill(items, focusIndex = 0) {
    menu.replaceChildren();
    items.forEach(([name, run, disabled, checked]) => {
      const b = document.createElement("button");
      b.textContent = name;
      b.type = "button";
      b.setAttribute("role", checked === undefined ? "menuitem" : "menuitemradio");
      b.tabIndex = -1;
      if (checked !== undefined) b.setAttribute("aria-checked", String(checked));
      if (run === "tiers") b.setAttribute("aria-haspopup", "menu");
      if (name.startsWith("Delete") && items.length > 1) b.className = "deleteAction startGroup";
      else if (name.startsWith("Delete")) b.className = "deleteAction";
      if (name === "None") b.className = "startGroup";
      if (disabled) {
        b.disabled = true;
        b.title = disabled;
      }
      b.onclick = () => {
        if (run === "tiers") return showTierMenu();
        closeProfileMenu();
        run();
      };
      menu.append(b);
    });
    const enabled = [...menu.querySelectorAll("button:not(:disabled)")];
    (enabled[focusIndex] || enabled[0] || menu).focus({ preventScroll: true });
  }
  function showTierMenu() {
    fill(
      [...tiers, "None"].map((t) => [
        t,
        () => assignTier(id, t),
        "",
        profileMeta[id].tier === t || (t === "None" && !tiers.includes(profileMeta[id].tier)),
      ]),
    );
  }
  menu.tabIndex = -1;
  fill(actions);
  menu.style.left =
    Math.max(4, Math.min(pointer ? event.clientX : rect.left, innerWidth - menu.offsetWidth - 5)) + "px";
  menu.style.top =
    Math.max(4, Math.min(pointer ? event.clientY : rect.bottom + 3, innerHeight - menu.offsetHeight - 5)) + "px";
  menu.onkeydown = (e) => {
    const items = [...menu.querySelectorAll("button:not(:disabled)")],
      current = items.indexOf(document.activeElement);
    const next = {
      ArrowDown: (current + 1) % items.length,
      ArrowUp: (current - 1 + items.length) % items.length,
      Home: 0,
      End: items.length - 1,
    }[e.key];
    if (next !== undefined && items.length) {
      e.preventDefault();
      items[next].focus();
    }
    if (e.key === "ArrowRight" && document.activeElement?.getAttribute("aria-haspopup") === "menu") {
      e.preventDefault();
      showTierMenu();
    }
    if (e.key === "ArrowLeft" && menu.querySelector("[role=menuitemradio]")) {
      e.preventDefault();
      fill(actions);
      [...menu.querySelectorAll("button")].find((b) => b.getAttribute("aria-haspopup"))?.focus();
    }
    if (e.key === "Escape" || e.key === "Tab") {
      if (e.key === "Escape") e.preventDefault();
      closeProfileMenu();
    }
  };
}
// Adaptive picks one profile per tier and GPU; an explicit assignment
// moves the tier to this profile. None removes it from Adaptive.
function assignTier(id, tier) {
  const m = profileMeta[id];
  m.tier = tier;
  m.assigned = tier === "None" ? 0 : Date.now();
  render();
  document.querySelector('[data-profile="' + id + '"]')?.focus({ preventScroll: true });
}
document.addEventListener("pointerdown", (e) => {
  if (!e.target.closest("#profileActions,#moreProfileActions")) closeProfileMenu(false);
});
document.addEventListener("keydown", (e) => {
  const trigger = e.target.closest?.("[data-profile]");
  if (trigger && (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")))
    openProfileMenu(e, Number(trigger.dataset.profile));
});
window.addEventListener("resize", () => closeProfileMenu(false));
document.addEventListener(
  "scroll",
  (e) => {
    if (!e.target.closest?.("#profileActions")) closeProfileMenu(false);
  },
  true,
);
