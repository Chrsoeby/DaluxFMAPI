"""
workorder_app.py — a simple, menu-driven terminal app for Dalux FM.

No IT knowledge needed: numbered menus, plain questions, IDs handled for you.
It can LIST existing records (work orders, tickets, estates, buildings, assets)
and ADD new ones (work orders, tickets).

Run it with:
    python3 workorder_app.py
or just double-click the "Dalux Work Orders.command" file.
"""

import sys
from datetime import datetime, timedelta, date

# Make Unicode (emoji, æøå) print safely on every platform, including older
# Windows consoles whose default code page can't encode emoji.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from dalux_fm import DaluxFMClient, DaluxFMError, DaluxFMAPIError


# --------------------------------------------------------------------------- #
# Pretty printing
# --------------------------------------------------------------------------- #
def hr():
    print("-" * 70)

def header(title):
    print("\n" + "=" * 70)
    print(f"   {title}")
    print("=" * 70)

def pause():
    input("\nPress Enter to go back...")

def table(columns, rows):
    """columns = [(title, width), ...]; rows = list of tuples."""
    print("".join(f"{t:<{w}}" for t, w in columns))
    hr()
    for r in rows:
        line = ""
        for value, (_, w) in zip(r, columns):
            text = "" if value is None else str(value)
            line += f"{text[:w - 1]:<{w}}"
        print(line)


# --------------------------------------------------------------------------- #
# Forgiving input helpers
# --------------------------------------------------------------------------- #
def ask_text(prompt, *, default=None, required=False):
    suffix = f" [{default}]" if default else ""
    while True:
        val = input(f"{prompt}{suffix}: ").strip()
        if not val and default is not None:
            return default
        if not val and not required:
            return ""
        if val:
            return val
        print("  (this one is required — please type something)")

def ask_yes_no(prompt, *, default=True):
    d = "Y/n" if default else "y/N"
    while True:
        val = input(f"{prompt} ({d}): ").strip().lower()
        if not val:
            return default
        if val in ("y", "yes"):
            return True
        if val in ("n", "no"):
            return False
        print("  please answer y or n")

def ask_date(prompt, *, default_days_from_now=0):
    default = (date.today() + timedelta(days=default_days_from_now)).isoformat()
    while True:
        val = input(f"{prompt} (YYYY-MM-DD) [{default}]: ").strip() or default
        if "T" in val:
            return val if val.endswith("Z") else val + "Z"
        try:
            datetime.strptime(val, "%Y-%m-%d")
            return f"{val}T12:00:00Z"
        except ValueError:
            print("  please use the format 2026-07-01")

def pick(prompt, items, label_fn, value_fn, *, allow_skip=False, default_value=None):
    print(f"\n{prompt}")
    if not items:
        print("  (none available)")
        return None
    for i, it in enumerate(items, 1):
        star = "  (default)" if default_value is not None and str(value_fn(it)) == str(default_value) else ""
        print(f"  {i}) {label_fn(it)}{star}")
    if allow_skip:
        print("  0) (skip / none)")
    while True:
        raw = input("Choose a number: ").strip()
        if not raw and default_value is not None:
            return default_value
        if allow_skip and raw == "0":
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return value_fn(items[int(raw) - 1])
        print("  please type one of the numbers shown")

def menu(title, labels):
    """Show a menu; return the chosen 1-based index, or len(labels) for the last
    item if the user just quits out. Returns int."""
    header(title)
    for i, label in enumerate(labels, 1):
        print(f" {i}) {label}")
    while True:
        raw = input("\nChoose a number: ").strip().lower()
        if raw in ("q", "quit", "exit"):
            return len(labels)
        if raw.isdigit() and 1 <= int(raw) <= len(labels):
            return int(raw)
        print(f"  please type 1-{len(labels)}")


# --------------------------------------------------------------------------- #
# Small data helpers
# --------------------------------------------------------------------------- #
def fmt_address(addr):
    if not addr:
        return "-"
    road = " ".join(x for x in [addr.get("road"), addr.get("number")] if x)
    city = " ".join(x for x in [addr.get("zipCode"), addr.get("city")] if x)
    return ", ".join(x for x in [road, city] if x) or "-"


# --------------------------------------------------------------------------- #
# The app
# --------------------------------------------------------------------------- #
class App:
    def __init__(self):
        self.fm = DaluxFMClient()       # embedded sandbox key
        self._lk = None

    # -- cached id->name lookup maps -- #
    def lk(self):
        if self._lk is None:
            def m(items, k):
                return {str(i.get(k)): i.get("name") for i in items}
            fm = self.fm
            self._lk = {
                "wo_status":     m(fm.workorder_statuses(), "workOrderStatusId"),
                "priority":      m(fm.workorder_priorities(), "priorityId"),
                "team":          m(fm.workorder_teams(), "teamId"),
                "building":      m(fm.buildings.list(), "buildingId"),
                "ticket_status": m(fm.ticket_statuses(), "ticketStatusId"),
                "topic":         m(fm.ticket_topics(), "topicId"),
            }
        return self._lk

    def _buildings_of(self, record):
        refs = (record.get("placement") or {}).get("buildingRefs") or []
        names = [self.lk()["building"].get(str(r.get("buildingId")), r.get("buildingId")) for r in refs]
        return ", ".join(str(n) for n in names) or "-"

    # =================================================================== #
    # LIST actions
    # =================================================================== #
    def list_workorders(self):
        header("Work orders")
        L = self.lk()
        rows = list(self.fm.workorders.iter())
        if not rows:
            print("  (none yet)"); return pause()
        table(
            [("No.", 8), ("Name", 30), ("Status", 11), ("Team", 16), ("Deadline", 12)],
            [(w.get("number"), w.get("name"),
              L["wo_status"].get(str(w.get("status")), w.get("status")),
              L["team"].get(str((w.get("teamRef") or {}).get("teamId")), "-"),
              (w.get("deadlineDate") or "")[:10]) for w in rows],
        )
        print(f"\nTotal: {len(rows)} work orders")
        pause()

    def list_tickets(self):
        header("Tickets")
        L = self.lk()
        rows = list(self.fm.tickets.iter())
        if not rows:
            print("  (none yet)"); return pause()
        table(
            [("No.", 8), ("Topic", 16), ("Status", 11), ("Building", 18), ("Description", 30)],
            [(t.get("number"),
              L["topic"].get(str((t.get("topic") or {}).get("topicId")), "-"),
              L["ticket_status"].get(str(t.get("status")), t.get("status")),
              self._buildings_of(t),
              t.get("description")) for t in rows],
        )
        print(f"\nTotal: {len(rows)} tickets")
        pause()

    def list_estates(self):
        header("Estates")
        rows = list(self.fm.estates.iter())
        if not rows:
            print("  (none yet)"); return pause()
        table(
            [("ID", 8), ("Name", 40)],
            [(e.get("estateId"), e.get("name")) for e in rows],
        )
        print(f"\nTotal: {len(rows)} estates")
        pause()

    def list_buildings(self):
        header("Buildings")
        rows = list(self.fm.buildings.iter())
        if not rows:
            print("  (none yet)"); return pause()
        table(
            [("ID", 8), ("Name", 28), ("Address", 34)],
            [(b.get("buildingId"), b.get("name"), fmt_address(b.get("address"))) for b in rows],
        )
        print(f"\nTotal: {len(rows)} buildings")
        pause()

    def list_assets(self):
        header("Assets")
        rows = list(self.fm.assets.iter())
        if not rows:
            print("  (none yet)"); return pause()
        def cls(a):
            c = a.get("classification") or {}
            c = c.get("data", c)
            return c.get("name", "-")
        table(
            [("ID", 8), ("Name", 26), ("Classification", 20), ("Building", 18)],
            [(a.get("assetId"), a.get("name"), cls(a), self._buildings_of(a)) for a in rows],
        )
        print(f"\nTotal: {len(rows)} assets")
        pause()

    def list_menu(self):
        actions = [
            ("Work orders", self.list_workorders),
            ("Tickets", self.list_tickets),
            ("Estates", self.list_estates),
            ("Buildings", self.list_buildings),
            ("Assets", self.list_assets),
            ("Back", None),
        ]
        while True:
            choice = menu("List existing...", [a[0] for a in actions])
            fn = actions[choice - 1][1]
            if fn is None:
                return
            fn()

    # =================================================================== #
    # ADD actions
    # =================================================================== #
    def add_workorder(self):
        header("Add a new work order")
        fm = self.fm
        print("Answer the questions below. Press Enter to accept a [default].\n")
        templates = fm.collect("2.0/workorders/templates")
        template_id = pick("Which template?", templates,
                           lambda t: t["name"], lambda t: t["templateId"], default_value="5")
        team_id = pick("Which team?", fm.workorder_teams(),
                       lambda t: t["name"], lambda t: t["teamId"])
        priority_id = pick("Which priority?", fm.workorder_priorities(),
                           lambda p: p["name"], lambda p: p["priorityId"], default_value="2")
        building_id = pick("Which building? (optional)", fm.buildings.list(),
                           lambda b: b["name"], lambda b: b["buildingId"], allow_skip=True)
        name = ask_text("Title of the work order", required=True)
        description = ask_text("Description", default="")
        start = ask_date("Start date", default_days_from_now=0)
        deadline = ask_date("Deadline date", default_days_from_now=14)
        responsible = ask_text("Assign to (email) — Enter to assign to yourself", default="")

        header("Please confirm")
        L = self.lk()
        print(f"  Template   : {name_of(templates, 'templateId', template_id)}")
        print(f"  Team       : {L['team'].get(str(team_id))}")
        print(f"  Priority   : {L['priority'].get(str(priority_id))}")
        print(f"  Building   : {L['building'].get(str(building_id), '(none)')}")
        print(f"  Title      : {name}")
        print(f"  Description: {description or '(none)'}")
        print(f"  Start      : {start[:10]}    Deadline: {deadline[:10]}")
        print(f"  Assign to  : {responsible or '(yourself)'}")
        if not ask_yes_no("\nCreate this work order? It cannot be deleted afterwards", default=False):
            print("  Cancelled — nothing was created."); return pause()

        kwargs = dict(template_id=template_id, team_id=team_id, priority_id=priority_id,
                      start_date=start, deadline_date=deadline, name=name)
        if description:
            kwargs["description"] = description
        if building_id:
            kwargs["building_ids"] = [building_id]
        if responsible:
            kwargs["responsible_email"] = responsible
        try:
            wo = fm.create_workorder(**kwargs)
            print(f"\n  ✅ Created work order number {wo.get('number')}.")
        except DaluxFMAPIError as e:
            self._friendly_error(e)
        pause()

    def add_ticket(self):
        header("Add a new ticket")
        fm = self.fm
        print("Answer the questions below.\n")
        topic_id = pick("Which topic?", fm.ticket_topics(),
                        lambda t: t["name"], lambda t: t["topicId"])
        # only buildings valid for this topic
        topic_buildings = fm.collect(f"2.0/tickets/topics/{topic_id}/buildings")
        building_id = pick("Which building?", topic_buildings,
                           lambda b: b["name"], lambda b: b["buildingId"])
        team_id = pick("Assign to which team? (optional)", fm.workorder_teams(),
                       lambda t: t["name"], lambda t: t["teamId"], allow_skip=True)
        description = ask_text("Describe the issue", required=True)
        reporter_email = ask_text("Your email (reporter)", required=True)

        header("Please confirm")
        L = self.lk()
        print(f"  Topic      : {L['topic'].get(str(topic_id))}")
        print(f"  Building   : {L['building'].get(str(building_id), building_id)}")
        print(f"  Team       : {L['team'].get(str(team_id), '(auto-assign)') if team_id else '(auto-assign)'}")
        print(f"  Reporter   : {reporter_email}")
        print(f"  Description: {description}")
        if not ask_yes_no("\nCreate this ticket? It cannot be deleted afterwards", default=False):
            print("  Cancelled — nothing was created."); return pause()

        extra = {}
        if team_id:
            extra["teamRef"] = {"teamId": str(team_id)}
        try:
            t = fm.create_ticket(topic_id, reporter_email, description,
                                 building_ids=[building_id], **extra)
            print(f"\n  ✅ Created ticket number {t.get('number')}.")
        except DaluxFMAPIError as e:
            self._friendly_error(e)
        pause()

    def add_menu(self):
        actions = [
            ("Work order", self.add_workorder),
            ("Ticket", self.add_ticket),
            ("Back", None),
        ]
        while True:
            choice = menu("Add new...", [a[0] for a in actions])
            fn = actions[choice - 1][1]
            if fn is None:
                return
            fn()

    # =================================================================== #
    def _export(self, which):
        import dalux_excel
        fn, stem = dalux_excel.EXPORTERS[which]
        out = f"{stem}_{date.today().isoformat()}.xlsx"
        header(f"Export {which} to Excel")
        try:
            n = fn(self.fm, out)
            print(f"  ✅ Wrote {n} {which} to: {out}")
            print(f"     (saved in the same folder as this app)")
        except Exception as e:
            print(f"  ⚠️  Export failed: {e}")
        pause()

    def export_menu(self):
        actions = [
            ("Work orders", lambda: self._export("workorders")),
            ("Tickets", lambda: self._export("tickets")),
            ("Estates", lambda: self._export("estates")),
            ("Buildings", lambda: self._export("buildings")),
            ("Assets", lambda: self._export("assets")),
            ("Back", None),
        ]
        while True:
            choice = menu("Export to Excel...", [a[0] for a in actions])
            fn = actions[choice - 1][1]
            if fn is None:
                return
            fn()

    def _friendly_error(self, e):
        msg = e.body.get("message") if isinstance(e.body, dict) else e.body
        print(f"\n  ⚠️  Could not create it: {msg}")
        if "was not found" in str(msg):
            print("      (An email you entered must be an existing Dalux user.)")

    # =================================================================== #
    def run(self):
        try:
            self.fm.workorders.count()          # connectivity check
        except DaluxFMError as e:
            print("\nCould not reach Dalux. Check your internet connection.")
            print(f"Details: {e}")
            return
        actions = [
            ("List existing...", self.list_menu),
            ("Add new...", self.add_menu),
            ("Export to Excel...", self.export_menu),
            ("Quit", None),
        ]
        while True:
            choice = menu("Dalux FM", [a[0] for a in actions])
            fn = actions[choice - 1][1]
            if fn is None:
                print("\nGoodbye!\n"); return
            fn()


def name_of(items, key, value):
    for it in items:
        if str(it.get(key)) == str(value):
            return it.get("name")
    return value


if __name__ == "__main__":
    try:
        App().run()
    except (KeyboardInterrupt, EOFError):
        print("\n\nGoodbye!\n")
        sys.exit(0)
