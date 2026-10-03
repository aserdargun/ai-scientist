# Kabul sırasında ayrılan kontrol ve cleanup kapasitesi

1 Ekim 2026. Scientist kaynak çalışması; native/GPU veya ortak runtime
admission değildir. Başlangıç HEAD `a60e9e2a2c872be97f696d1dada130d502d2ad6c`.
AOS salt okunur incelendi; AOS dosyası/servisi değiştirilmedi.

## Yakalanan hata

100000 kontrol-ID kaydı tavanında yeni status/cancel/reconcile/capability
istekleri mevcut deney için de `busy` oluyordu. 40 kayıt/4 grant küçük CPU
fixture sınırlarıyla, current/retired hedef ve dört işlem kombinasyonu:
**8 failed / 2.10s**, ana süreç exit1 / 2.285776356s; Python kaynakları sabitti.
Bu sentetik SQLite hata kanıtıdır; aktif GPU işi durdurulmadı.

## Çözüm

Yeni intent veya cancel-before-intent tombstone kabulünde aynı SQLite
transaction içinde özgün bütçeye bağlı değişmez rezervasyon ayrılır:

```text
N = ceil((original_queue_seconds + original_total_seconds + 30) / 60) + 2
N <= 40
capability: N   status: N   reconcile: N   cancel: 2   cleanup_grants: 8
```

Bütçe ve kota canonical hash ile bağlanır; exact retry mevcut kotayı ve
harcanmış payları korur. Global hesap durable kullanılan kayıtlar ve tüm
harcanmamış rezervlerin toplamıdır. Tam rezerv sığmıyorsa yeni infer kabulü
model veya scheduler etkilerinden önce reddedilir. Audit kayıtları silinmez;
status/refresh cancel veya reconcile payını harcayamaz.

Hedefe bağlı capability yanıtı ayrılan kontrol-ID kaydında kalıcı saklanır;
genel infer cache doluluğu bu yolu engellemez. Aynı ID aynı eski yanıtı ve
eski expiry'yi döndürür; süresi dolmuş yanıt işlem yetkisi sağlamaz. Bu
kontrol capability'si infer cache'e girmez ve yeni infer başlatamaz.
Retired-policy grant mevcut açık hedef ACL'sini ve özgün kimlikleri gerektirir;
onun kapasitesi de ayrı rezerve edilir. Denied/revoked veya geçersiz istek
rollback olur; kontrol/grant/kota payını tüketmez.

Bu sınırlı bir güvence, sonsuz sorgu hakkı değildir. Ayrı yeni ID'lerle
sorgulayan istemci 60 saniyeden hızlı polling yapmamalı; aynı capability
penceresinde exact status ID tekrarını kullanabilir. Özgün kuyruk+turn+drain
ve iki recovery payı bittiğinde yeni farklı ID `busy` olur. Terminal olmak
kota veya deadline'ı yenilemez. Eski rezervsiz kayıtların cleanup'ı yalnız
boş, rezerve edilmemiş kapasitede best effort'tür; bunlara yeni infer yetkisi
veya geriye dönük bütçe verilmez. Caller generation değişimi hâlâ ayrı açık
sözleşme maddesidir.

## Kontrol kanıtları

İlk birleşik kaynak kontrolü **178 passed / 13.71s**, ana süreç exit0 /
13.918906431s; Python kaynakları sabitti. Current/retired hedef, dolu infer
cache, quota/grant tavanı, yanlış/revoked hedef, rollback, aynı generation'da
controller nesnesi yeniden kurulması, yeni resolver generation, exact retry
ve negatif reservation corruption senaryolarını içerir. Nesne yeniden kurulması
gerçek süreç restart veya native GPU toparlanması kanıtı değildir.

Son incelemede geç policy revoke yarışı da yakalandı: SQL mutation sonrasında
son peer gözleminde policy kaldırılınca status/infer commit'i reddedilmiyordu.
**2 failed / 0.21s**, ana süreç exit1 / 0.391072486s; Python kaynakları sabitti.
Son bloklanabilen peer gözleminden sonra, freshness kontrolünden önce bounded
policy-hash yeniden doğrulaması eklendi. Infer, ordinary/retired control,
hedef capability refresh ve bootstrap yolları bu kontrolü kullanır; ikinci
source taraması eklenmedi. Düzeltme sonrası **180 passed / 12.45s**, ana süreç
exit0 / 12.640670479s, Python kaynakları sabitti. Nihai zorunlu kapıda yedi komutun tamamı exit0; ana süreç exit0 /
102.030801690s; 1640 passed / 7 opt-in skipped / 120 GPU-live deselected.
Python kaynak hash'leri değişmedi ve son kaynaklarla birebir karşılaştırıldı.
Strict mypy, Ruff, Pylint, Bandit ve wheel içerik/import kontrolleri geçti.

İlk tam kapıda pytest başarılıydı; Bandit'in dinamik SQL ve strict mypy'nin
tip kontrolleri başarısızdı. Bunlar literal SQL ve açık tip/kapalı hata
kontrolleriyle düzeltildi; hata sonuçları başarı olarak sunulmaz.

## Açık gerçek kabul

AOS hâlen kapalı dört alanlı admission profil pini kullanıyor; [note84](84-aos-admission-profile-pin-compatibility.md)
beş alanlı tam şemayı ve dar geçiş adımlarını verir. Ortak bundle/schema/version
teyidi, tam control/terminal resolution ve uygun scheduler rezervasyonu
olmadan gerçek AOS → Scientist → rapor/GPU devri çalıştırılmaz.

Stop ACK veya idle release kanıtı değildir. Fiziksel cleanup kanıtlanmazsa
mevcut quarantine ve tahsis korunur. Bu kaynak tesliminde model/quantization,
VRAM tepesi ve GPU bekleme/devir/çalışma gecikmeleri ölçülmedi. Lisans ve genel
CI ayrı işlerdir; bu teslimat onları kapatmaz.

Kontrol descriptor SHA (tam JSON Schema değil):
`110838e17f1ea899067d9c9ec61769b6fe26a607ecac986f83786b9f8943068d`. Infer altı alanlı wire1 olarak kaldı; infer pin
SHA `a5aada91670f3741d822148ebc5bf9a2d4775362bda66bc4825def0960338d14`. Çıktı bundle
pini note83 ile aynıdır. Yeni descriptor/kota semantiği ortak runtime kabulü
olarak sunulmaz; counterpart bu kesin hash'i de teyit etmelidir.
