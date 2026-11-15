// Small progressive enhancements for the admin pages (no external libraries).
(function () {
  // Confirm dialogs for destructive actions: <form data-confirm="...">
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      if (!window.confirm(f.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // Participant search on the certificate form.
  var box = document.querySelector("[data-participant-search]");
  if (box) {
    var input = box.querySelector("input[type=search]");
    var hidden = document.querySelector("input[name=participant_id]");
    var chosen = document.querySelector("[data-chosen]");
    var list = document.createElement("ul");
    list.hidden = true;
    box.appendChild(list);
    var timer;
    input.addEventListener("input", function () {
      clearTimeout(timer);
      var q = input.value.trim();
      if (q.length < 2) { list.hidden = true; return; }
      timer = setTimeout(function () {
        fetch("/admin/participants/search.json?q=" + encodeURIComponent(q), { credentials: "same-origin" })
          .then(function (r) { return r.json(); })
          .then(function (rows) {
            list.innerHTML = "";
            rows.forEach(function (row) {
              var li = document.createElement("li");
              var b = document.createElement("button");
              b.type = "button";
              b.textContent = row.label;
              b.addEventListener("click", function () {
                hidden.value = row.id;
                chosen.textContent = "Selected: " + row.label;
                list.hidden = true;
                input.value = "";
              });
              li.appendChild(b);
              list.appendChild(li);
            });
            list.hidden = rows.length === 0;
          });
      }, 200);
    });
    var clear = document.querySelector("[data-clear-participant]");
    if (clear) clear.addEventListener("click", function () { hidden.value = ""; chosen.textContent = "No participant selected"; });
  }

  // Course select -> show its default duration as the hours placeholder.
  var course = document.querySelector("select[name=course_id][data-hours]");
  var hours = document.querySelector("input[name=duration_hours]");
  if (course && hours) {
    var sync = function () {
      var opt = course.options[course.selectedIndex];
      hours.placeholder = opt && opt.dataset.hours ? opt.dataset.hours + " (course default)" : "";
    };
    course.addEventListener("change", sync); sync();
  }

  // Auto-refresh while an import job is processing.
  if (document.querySelector("[data-autorefresh]")) setTimeout(function () { location.reload(); }, 2000);
})();
