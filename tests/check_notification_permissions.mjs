// Run after: npm install --prefix output/notification-sql-check --no-audit --no-fund @electric-sql/pglite
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const { PGlite } = await import(new URL(
  "../output/notification-sql-check/node_modules/@electric-sql/pglite/dist/index.js", import.meta.url,
));
const db = new PGlite();
const owner = "11111111-1111-4111-8111-111111111111";
const other = "22222222-2222-4222-8222-222222222222";
try {
  await db.exec(`
    create role anon;
    create role authenticated;
    create role service_role bypassrls;
    create schema auth;
    create table auth.users (id uuid primary key);
    create function auth.uid() returns uuid language sql stable as
      $$select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid$$;
    grant usage on schema auth to anon, authenticated, service_role;
    insert into auth.users values ('${owner}'), ('${other}');
  `);
  await db.exec(await readFile(new URL(
    "../supabase/migrations/20261108153423_meeting_notifications.sql", import.meta.url,
  ), "utf8"));
  await db.exec("set role service_role");
  for (const user of [owner, other]) {
    await db.query(`insert into public.meeting_notifications
      (user_id,event_key,kind,title,body) values ($1,'event','completed','Ready','Saved')`, [user]);
  }
  await db.exec("reset role; set role authenticated");
  await db.query("select set_config('request.jwt.claim.sub',$1,false)", [owner]);
  assert.equal((await db.query("select * from public.meeting_notifications")).rows.length, 1);
  await db.query("update public.meeting_notifications set read_at=now() where user_id=$1", [other]);
  await db.query("update public.meeting_notifications set read_at=now() where user_id=$1", [owner]);
  await assert.rejects(db.query(`insert into public.meeting_notifications
    (user_id,event_key,kind,title,body) values ($1,'forged','failed','Fake','Fake')`, [other]), /row-level security/);
  await assert.rejects(db.query("update public.meeting_notifications set body='Changed'"), /permission denied/);
  await assert.rejects(db.query("update public.meeting_notifications set user_id=$1", [other]), /permission denied/);
  // The API's ignore_duplicates upsert must work without broad UPDATE grants.
  await db.query(`insert into public.meeting_notifications
    (user_id,event_key,kind,title,body) values ($1,'event','completed','Ready','Saved')
    on conflict (user_id,event_key) do nothing`, [owner]);
  assert.ok((await db.query("select read_at from public.meeting_notifications")).rows[0].read_at);
  await db.exec("reset role");
  assert.equal((await db.query("select read_at from public.meeting_notifications where user_id=$1", [other])).rows[0].read_at, null);
  await db.exec("set role anon");
  await assert.rejects(db.query("select * from public.meeting_notifications"), /permission denied/);
  console.log("Notification migration and PostgreSQL role/RLS checks passed.");
} finally {
  await db.close();
}
