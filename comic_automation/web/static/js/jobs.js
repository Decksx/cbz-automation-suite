// Read-only job history. Pages replace earlier rows so long histories never
// accumulate an unbounded DOM. All text uses h()/textContent, including errors.
import { api } from "./api.js";
import { h } from "./h.js";

export function startJobs() {
  const $ = (id) => document.getElementById(id);
  let busy = false;
  let next = null;
  let nextEvent = null;
  let selected = null;

  function controls() {
    for (const button of $("jobs").querySelectorAll("button")) button.disabled = busy;
    $("jobs-older").disabled = busy || next === null;
    $("job-events-more").disabled = busy || nextEvent === null;
    $("jobs").setAttribute("aria-busy", String(busy));
  }

  async function request(message, work) {
    if (busy) return;
    busy = true;
    $("jobs-notice").hidden = true;
    $("jobs-status").textContent = message;
    controls();
    try {
      await work();
    } catch (err) {
      $("jobs-notice").textContent = err.status === 401
        ? "Your sign-in expired. Open the console from its shortcut again."
        : `Could not load job history: ${err.message}`;
      $("jobs-notice").hidden = false;
      $("jobs-status").textContent = "History could not be refreshed.";
    } finally {
      busy = false;
      controls();
    }
  }

  const time = (value) => new Date(value).toLocaleString();

  function detail(id, after = null) {
    return request("Loading job details…", async () => {
      const query = after === null ? "" : `?after=${encodeURIComponent(after)}`;
      const data = await api("GET", `/api/jobs/${encodeURIComponent(id)}${query}`);
      selected = id;
      nextEvent = data.next;
      $("job-detail").hidden = false;
      const job = data.job;
      const facts = [["Job",job.job_id],["Tool",job.tool_id],["Status",job.state],
        ["Mode",job.dry_run ? "Dry run" : job.readonly ? "Read only" : "Normal run"],
        ["Requested",time(job.admitted_ms)],["Updated",time(job.updated_ms)]];
      $("job-facts").replaceChildren(...facts.flatMap(([key,value]) => [h("dt",{},key),h("dd",{},value)]));
      $("job-events").replaceChildren(...data.events.map((event) =>
        h("li",{},`${time(event.at_ms)} — ${event.event.replaceAll("_"," ")}`)));
      $("job-events-empty").hidden = data.events.length !== 0;
      $("jobs-status").textContent = `Showing ${data.events.length} ${data.events.length === 1 ? "event" : "events"} for ${job.tool_id}.`;
      if (after === null) $("job-detail-heading").focus();
    });
  }

  function list(before = null) {
    return request("Loading jobs…", async () => {
      const query = before === null ? "" : `?before=${encodeURIComponent(before)}`;
      const data = await api("GET", `/api/jobs${query}`);
      next = data.next;
      $("job-rows").replaceChildren(...data.jobs.map((job) => {
        const button = h("button",{type:"button",class:"job-link"},job.tool_id);
        button.addEventListener("click",() => detail(job.job_id));
        return h("tr",{},h("td",{},button,h("small",{class:"job-id"},job.job_id)),
          h("td",{},job.state.replaceAll("_"," ")),
          h("td",{},job.dry_run ? "Dry run" : job.readonly ? "Read only" : "Normal run"),
          h("td",{},time(job.updated_ms)));
      }));
      $("jobs-empty").hidden = data.jobs.length !== 0;
      $("jobs-status").textContent = `Showing ${data.jobs.length} ${data.jobs.length === 1 ? "job" : "jobs"}${before === null ? ", newest first" : " from an older page"}.`;
    });
  }

  $("jobs-refresh").addEventListener("click",() => list());
  $("jobs-older").addEventListener("click",() => list(next));
  $("job-events-more").addEventListener("click",() => detail(selected,nextEvent));
  return list();
}
