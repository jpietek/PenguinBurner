// The table and More actions share one nonmodal, keyboard-operable menu.
let profileMenuAnchor;
function closeProfileMenu(restore = true) {
  const menu = document.getElementById("profileActions");
  if (!menu) return;
  menu.remove();
  profileMenuAnchor?.setAttribute("aria-expanded", "false");
  if (restore) profileMenuAnchor?.focus({ preventScroll: true });
}
function openProfileMenu(event, id) {
  event.preventDefault();
  const more = event.currentTarget.id === "moreProfileActions";
  const previous = document.getElementById("profileActions");
  if (previous) {
    closeProfileMenu(false);
    if (more) return;
  }
  if (pi !== id) chooseProfile(id);
  profileMenuAnchor = more
    ? document.getElementById("moreProfileActions")
    : document.querySelector('[data-profile="' + id + '"]');
  if (!profileMenuAnchor) return;
  profileMenuAnchor.setAttribute("aria-expanded", "true");
  const rect = profileMenuAnchor.getBoundingClientRect();
  const pointer =
    event.type === "contextmenu" && (event.clientX || event.clientY);
  const menu = document.createElement("div");
  menu.id = "profileActions";
  menu.className = "contextMenu";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "Actions for " + profiles[id][0]);
  document.body.append(menu);
  const actions = [
    ["Edit V/F curve", () => openEditor("vf")],
    ["Edit fan curve", () => openEditor("fan")],
    ["Edit memory offset", memoryDialog],
    ["Assign adaptive tier", showTierMenu],
    ["Verify profile", verifyProfile],
    ["Export LACT", exportProfile],
    ["Restore GPU defaults", restoreDialog],
    ["Delete profile", () => confirmDelete([pi])],
  ];
  function fill(items) {
    menu.replaceChildren();
    items.forEach(([name, run, checked]) => {
      const b = document.createElement("button");
      b.textContent = name;
      b.type = "button";
      b.setAttribute(
        "role",
        checked === undefined ? "menuitem" : "menuitemradio",
      );
      b.tabIndex = -1;
      if (checked !== undefined)
        b.setAttribute("aria-checked", String(checked));
      if (name === "Assign adaptive tier")
        b.setAttribute("aria-haspopup", "menu");
      if (name === "Restore GPU defaults") b.className = "startGroup";
      if (name === "Delete profile") b.className = "deleteAction";
      b.onclick = () => {
        if (run !== showTierMenu) closeProfileMenu();
        run();
      };
      menu.append(b);
    });
    menu.querySelector("button").focus({ preventScroll: true });
  }
  function showTierMenu() {
    fill(
      [...tiers, "None"].map((t) => [
        t,
        () => {
          profileMeta[id].tier = t;
          render();
          document
            .querySelector('[data-profile="' + id + '"]')
            ?.focus({ preventScroll: true });
        },
        profileMeta[id].tier === t,
      ]),
    );
  }
  fill(actions);
  menu.style.left =
    Math.max(
      4,
      Math.min(
        pointer ? event.clientX : rect.left,
        innerWidth - menu.offsetWidth - 5,
      ),
    ) + "px";
  menu.style.top =
    Math.max(
      4,
      Math.min(
        pointer ? event.clientY : rect.bottom + 3,
        innerHeight - menu.offsetHeight - 5,
      ),
    ) + "px";
  menu.onkeydown = (e) => {
    const items = [...menu.querySelectorAll("button")],
      current = items.indexOf(document.activeElement);
    const next = {
      ArrowDown: (current + 1) % items.length,
      ArrowUp: (current - 1 + items.length) % items.length,
      Home: 0,
      End: items.length - 1,
    }[e.key];
    if (next !== undefined) {
      e.preventDefault();
      items[next].focus();
    }
    if (
      e.key === "ArrowRight" &&
      document.activeElement?.getAttribute("aria-haspopup") === "menu"
    ) {
      e.preventDefault();
      showTierMenu();
    }
    if (e.key === "ArrowLeft" && menu.querySelector("[role=menuitemradio]")) {
      e.preventDefault();
      fill(actions);
      menu.children[3].focus();
    }
    if (e.key === "Escape" || e.key === "Tab") {
      if (e.key === "Escape") e.preventDefault();
      closeProfileMenu();
    }
  };
}
document.addEventListener("pointerdown", (e) => {
  if (!e.target.closest("#profileActions,#moreProfileActions"))
    closeProfileMenu(false);
});
document.addEventListener("keydown", (e) => {
  const trigger = e.target.closest("[data-profile]");
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
