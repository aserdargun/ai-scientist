# Yerel öneri sözdizimi ve AOS yaşam döngüsü incelemesi

Tarih: 2026-09-25. Başlangıç: `4932f35`, harness 0.22.0. Üç Luna/high
kolu public süit, holdout ve AOS yaşam döngüsünde ilerliyor; root kaynak
birleştirmesi ve gerçek donanım ölçümünü yürütüyor.

## 0.23 yerel öneri onarımı

0.21 gerçek S2 tekrarı JSON sözleşmesini geçmiş, Python AST'sinde
reddedilmişti. Üretim provider'ı yalnız JSON/şema/hareket kontrolündeki
hataları tek onarım yoluna alıyordu; geçersiz Python bu denetimi atlıyordu.
Hatanın kaynak metni saklanmadığı için kesilme veya escape nedeni çıkarılmadı.

Luna'nın iki dosyalık dilimi güncel main hash'leriyle eşleştirilip alındı:
`lab/director/local_llm.py`, `tests/test_local_qwen_provider.py`.
`qwen-ast-repair-023-source.json` başlangıç/son hash'leri tutar. Provider
artık kaynağı çalıştırmadan `ast.parse` ile denetler. SyntaxError yalnız
satır/sütun bilgisine dönüşür; üretilen kaynak hata mesajına girmez.
Mevcut tek onarım, aynı strict şema ve toplam süre/token bütçesi kullanılır.
Davranış değişikliği `one-schema-and-python-syntax-repair.v2` config hash'ine
bağlıdır. Yerel kaynak sınırı 6000, hipotez sınırı 384 karakterdir.

İzole hedefli testler 16/exit 0; birleşik 0.23 imaj ve tam kapı
**session 19551 / exit 0**: **292 passed, 7 skipped, 13 deselected**,
strict mypy **77 dosya**, Pylint **9.35**, yedi komut başarılı.
85 runtime dosyasında kaynak/imaj byte eşliği doğrulandı.
İmaj `sha256:cd3cc6b4c2467630b2a2c1fc315abf7a29cf21af4e98130c25c9a50fbec18dc9`.
Bu CPU kanıtı gerçek S2 araştırma kabulü değildir.

Yeni `review_local_qwen_provider_turn.py`, tek ham model çağrısı yerine
üretim `propose_bounded` yolunu kullanır: sentetik metadata bağlamında bir
S2 bölümü, mevcut onarım/fallback, en fazla 600 saniye ve 24.576 toplam
model token rezervi; S2 çıktı toplamı en fazla 8192 token. Aday kodu
çalıştırılmaz, Scorer/araştırma/AOS görevi başlatılmaz. Provider receipt'leri,
kaynak uzunluğu/hash'i, GPU/süre/host ölçümleri kaydedilir; kaynak metni ve
düşünme içeriği kaydedilmez. Her model süreci aynı sınırlı GPU kuyruğu ve
bağımsız owned-process gözlemcisinden geçer. Gerçek sonuç aşağıda ayrıca
kaydedilir; başarılı sayılmadan exit code ve temizleme denetlenir.

Gerçek üretim-provider bölümü **session 16408 / exit 1**, dış süre
184,417 sn, provider süresi 181,973 sn verdi. İlk S2 çağrısı 512 giriş /
2837 çıktı token ile tamamlandı; onarım ikinci model aktivasyonundan önce
`ValueError` / `budget_exhausted` ile durdu. Geçerli öneri yoktur. Saklanan
receipt ayrıntılı preflight hata mesajını içermediği için hangi bütçe
denetiminin tetiklendiği bu kayıtla tek başına kesinleştirilmez. Kod
incelemesi, önceki yanıtı tam ekleyen onarım isteminin 2048 bağlam token
sınırını aşabilmesini gösteriyor; gerçek pinned tokenizer ile ayrı CPU
yeniden üretimi sonraki adımdır.

**11/13 kontrol** geçti: tek ticket `done`, model nesli/cgroup ve parent
kapandı, dış GPU tüketicisi oluşmadı, kaynak/runtime değişmedi. Gözlemci
GPU zirvesi 12854 MiB, receipt model zirvesi 12784 MiB, son toplam 62 MiB;
en düşük kullanılabilir RAM 19.486.490.624 bayt, en yüksek GPU sıcaklığı
63°C. Kanıt `local-qwen-provider-023-episode-review.json`; başarısız sonuç
korunur. Bu sonuç S2 veya birlikte araştırma kabulünü kapatmaz.

## İzole AOS başlangıç yarışı

Frozen AOS kopyasında normal `serve_desktop.py` shared-GPU hook'unu
kuruyor, fakat `create_console(..., lab_external_jobs=...)` bağlantısı yok.
Lab start/status/report yolları testte enjekte edilmiş koordinatörle
doğrulanmıştı; normal opt-in bootstrap hâlâ gereken ürün işidir.

Ek incelemede `LabExternalJobCoordinator.start()` yalnız model kararından
önce HUMAN lease/generation denetliyor. Karar beklenirken kontrol değişirse
sonraki intent/HTTP başlangıcı eski yetkiyle ilerleyebilir. Gerçek in-memory
SQLite migration'ları ve fixture controller/client/delayed decision ile
yeniden üretildi: generation 3→4 sonrası **bir Lab start çağrısı** ve bir
kalıcı dış iş oluştu; engellenmesi beklenen kontrol **exit 1** verdi.
Kanıt: `review-evidence/aos-lab-start-await-race-before.json`; driver
`review_aos_lab_start_race.py`. Kaynaklar değişmedi; ağ/model/masaüstü veya
canlı AOS işlemi yoktur.

Luna yeni public kaynak test kopyasında karar sonrasında güncel
session/runtime/lease/generation kontrolü, yürütme başlangıcında deadline,
normal opt-in bootstrap, restart/quiesce ve aktif iş sınırı üzerinde
çalışıyor. Masaüstü kontrol kilidi model beklemesi boyunca tutulmayacak;
Lab araştırması foreground scheduler slotunu işgal etmeyecek. Canlı AOS'a
aktarım paralel kaynaklarla uzlaştırılmadan yapılmaz. Düzeltme ve gerçek
birlikte çalışma kabulü henüz tamamlanmış sayılmaz.
