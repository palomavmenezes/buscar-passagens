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

document.addEventListener("DOMContentLoaded", () => {
  const trip = document.getElementById("trip_type");
  const nights = document.getElementById("nights-wrap");
  const modes = document.querySelectorAll("input[name='date_mode']");
  const syncNights = () => {
    if (!trip || !nights) return;
    const mode = document.querySelector("input[name='date_mode']:checked")?.value;
    const needsNights = trip.value === "round_trip" && mode !== "specific";
    nights.classList.toggle("hidden", !needsNights);
  };
  trip?.addEventListener("change", syncNights);
  modes.forEach((input) => input.addEventListener("change", syncNights));
  syncNights();
});
