function pollSearch(id) {
  const tick = async () => {
    const response = await fetch(`/buscas/${id}/status`);
    const data = await response.json();
    const progress = document.getElementById("progress");
    if (progress && data.progress) {
      progress.textContent = `${data.progress} · ${data.status}`;
    }
    if (data.status === "running" || data.status === "queued") {
      setTimeout(tick, 2000);
    } else {
      window.location.reload();
    }
  };
  setTimeout(tick, 1500);
}

const formatMiles = (value) => Number(value).toLocaleString("pt-BR");

document.addEventListener("DOMContentLoaded", () => {
  const navToggle = document.querySelector(".nav-toggle");
  const nav = document.getElementById("site-nav");
  navToggle?.addEventListener("click", () => {
    const open = nav?.classList.toggle("open");
    navToggle.setAttribute("aria-expanded", open ? "true" : "false");
  });

  const trip = document.getElementById("trip_type");
  const nights = document.getElementById("nights-wrap");
  const returnWrap = document.getElementById("return-date-wrap");
  const modes = document.querySelectorAll("input[name='date_mode']");
  const syncTripFields = () => {
    const round = trip?.value === "round_trip";
    if (returnWrap) {
      returnWrap.classList.toggle("hidden", !round);
      const input = returnWrap.querySelector("input");
      if (input) input.disabled = !round;
    }
    if (!nights) return;
    const mode = document.querySelector("input[name='date_mode']:checked")?.value;
    const needsNights = round && mode !== "specific";
    nights.classList.toggle("hidden", !needsNights);
  };
  trip?.addEventListener("change", syncTripFields);
  modes.forEach((input) => input.addEventListener("change", syncTripFields));
  syncTripFields();

  const table = document.getElementById("results-table");
  const sumEl = document.getElementById("combine-sum");
  const detailEl = document.getElementById("combine-detail");
  if (!table || !sumEl) return;

  const picked = { ida: null, volta: null };
  const render = () => {
    const ida = picked.ida;
    const volta = picked.volta;
    if (ida && volta) {
      const total = Number(ida.dataset.miles || 0) + Number(volta.dataset.miles || 0);
      sumEl.textContent = `${formatMiles(total)} milhas`;
      detailEl.textContent = `${ida.dataset.route} (${ida.dataset.when.trim()}) + ${volta.dataset.route} (${volta.dataset.when.trim()})`;
      return;
    }
    sumEl.textContent = "escolha os dois trechos";
    detailEl.textContent = "";
  };

  table.querySelectorAll("tbody tr").forEach((row) => {
    const kind = row.dataset.kind;
    if (kind !== "ida" && kind !== "volta") return;
    row.addEventListener("click", (event) => {
      if (event.target.closest("a")) return;
      const current = picked[kind];
      if (current) current.classList.remove(kind === "ida" ? "pick-ida" : "pick-volta");
      if (current === row) {
        picked[kind] = null;
      } else {
        picked[kind] = row;
        row.classList.add(kind === "ida" ? "pick-ida" : "pick-volta");
      }
      render();
    });
  });
});
