create table if not exists articles(id integer primary key, kind text, title text, body text, anime_id integer, created text default (datetime('now')));
create table if not exists comments(id integer primary key, article_id integer, name text, text text, created text default (datetime('now')));
create table if not exists poll(option text primary key, votes integer default 0);
create table if not exists hits(ip text, what text, t integer);
create index if not exists comments_by_article on comments(article_id);
create index if not exists articles_by_anime on articles(anime_id);
create index if not exists hits_by_ip on hits(ip, what, t);
insert or ignore into poll(option) values ('Monogatari: Off & Monster Season'),('Liberated from Paradise'),('Horror Collector');
insert into articles(kind,title,body) select 'News','Welcome to marimoblog','Your first article. Delete it from /admin.html and publish your own.' where not exists(select 1 from articles);
