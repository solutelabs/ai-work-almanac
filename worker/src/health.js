// What the live check reads: timestamps and counts only. No names, so a leaked token shows nothing about people.
const iso = (value) => (typeof value === "string" && Number.isFinite(Date.parse(value)) ? new Date(value).toISOString() : null);

// null means there is nothing to report, which the checker treats as a failure, never as fresh.
export function buildHealth({ gitSyncedAt, laptopSeen, laptopsLastDay }, now = Date.now()) {
  return {
    now: new Date(now).toISOString(),
    git_synced_at: iso(gitSyncedAt),
    last_laptop_seen: iso(laptopSeen),
    laptops_seen_24h: Number.isInteger(laptopsLastDay) && laptopsLastDay >= 0 ? laptopsLastDay : null,
  };
}

export async function handleHealth(sql) {
  const [git] = await sql`SELECT MAX(synced_at) AS at FROM tracker.git_activity`;
  const [laptops] = await sql`
    SELECT MAX(m.last_seen) AS seen, COUNT(*) FILTER (WHERE m.last_seen >= ${new Date(Date.now() - 86400000).toISOString()})::int AS recent
    FROM tracker.machines m JOIN tracker.developers d ON d.id = m.developer_id WHERE d.revoked_at IS NULL`;
  return new Response(JSON.stringify(buildHealth({ gitSyncedAt: git?.at, laptopSeen: laptops?.seen, laptopsLastDay: laptops?.recent })), {
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}
