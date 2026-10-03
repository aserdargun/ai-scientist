# 77 — Scientist AOS kontrol bağlantısı: kaynak teslimi

30 Eylül 2026. [75 önerisi](75-aos-control-contract-proposal.md) doğrultusunda
Scientist kaynaklarına kontrol kodu eklendi. **AOS ile ortak runtime kabulü
yapılmadı; servis yeniden başlatılmadı, kontrol endpoint'i etkinleştirilmedi.**

## Eklenen çalışma akışı

- Ayrı authenticated `capability`, `status`, `cancel`, `reconcile` kontrol
  listener'ı; dört inference worker'ından bağımsız iki kontrol worker'ı.
  Infer wire1'in altı alanı korunur. Eksik/yanlış private policy admission'ı
  kapalı tutar; capability, doğru deployment/profile/schema ve güncel peer'e
  bağlıdır.
- Mevcut tek arbiter SQLite DB'sinde immutable intent, cancel-before-intent
  tombstone ve terminal receipt. Yeni GPU allocator yok. İptal ACK'i tahsisi
  bırakmaz; özgün principal/generation/hash/bütçe/deadline korunur.
- Queued/acquire ve plan/start/go yarışları aynı transaction düzeniyle
  sıralanır. Trusted cleanup, bounded unallocated taramasıyla restart sonrası
  intent/queued kayıtlarını da uzlaştırır; iş başlatmaz veya bütçe yenilemez.
- Unbound planned/starting child için süre dolması release kanıtı kabul
  edilmez. Exact child/cgroup/GPU drain kanıtlanamıyorsa quarantine korunur.
  Terminal receipt ve scheduler release aynı transaction'da gerçekleşir.
- Caller restart eski sahipliği devralmaz. History reconciliation ayrı,
  varsayılan kapalı ACL ile redakte edilir. `unknown` veya lost ACK, inference
  tekrarına izin değildir.

## Kaynak ve doğrulama

Yeni modüller `lab/llm/aos_gpu_control.py` ve `aos_gpu_control_store.py`;
broker, executor, service ve mevcut scheduler aynı kayıt/otoriteye bağlandı.
İzole R4 patch SHA-256:
`dcb0971624108ff81997e36d189f38dd779779ed1144ecec564a511ef0294a3e`.

Root'un doğru izole import yollarını doğruladığı kısa CPU kontrolü:
**95 passed / pytest 0.58 saniye / parent exit 0, 0.793515424 saniye**.
Bu synthetic peer/process/GPU double kanıtıdır; gerçek GPU drain veya AOS
deployment kabulü değildir. R1/R3 fixture/path başarısızlıkları ve tüm gerçek
çıkış kodları ayrı receipt'lerde korundu. Public uygulamadan sonra biçim/SQL
literal düzenlemelerinde literal AST eşliği doğrulandı. Zorunlu genel kalite
kapısı **exit 0 / 84.518426297 saniye**: yedi komut exit 0; **1479 passed,
7 opt-in skipped, 120 GPU/live deselected**; strict mypy 148 kaynak dosyasında
temiz; wheel build ve wheel import geçti. İlk gate'te tip/assert bulguları ve
20 GiB reserve kuralına uygun olmayan `/tmp` filesystem seçimi kaydedildi ve
düzeltildi. Disk reserve veya diğer güvenlik kapıları gevşetilmedi.

[Kaynak ve wire/schema hash'leri](review-evidence/aos-control-source-delivery.json)
AOS oturumunun aynı sözleşmeyi doğrulaması için yayımlandı. Bunlar runtime
capability, deployment veya GPU admission receipt'i değildir.

## Açık kalan gerçek entegrasyon

AOS, 75'in sonundaki kontrol frame/hash, caller unit/principal, private socket,
deployment/profile/source pinleri ve history politikasını teyit etmelidir.
Hiçbir varsayılan canlı endpoint veya yetkili capability uydurulmadı. Ardından
tek scheduler rezervasyonu ve kullanıcı işi kontrolüyle Scientist'in yönettiği
sınırlı AOS → Scientist → AOS gerçek GPU kabulü gerekir.

Kalıcı tombstone/control-ID kapasitesi dolunca yeni admission kapanır; mevcut
trusted cleanup korunur. Yeni dış cancel/reconcile kayıtları kapasite dolunca
`busy` olabilir; sonsuz kullanılabilirlik iddiası yoktur. Lisans/genel CI işleri
ayrıdır. [R10 karantina kaydı](76-native-r10-quarantine-proof.md) değiştirilmedi;
bu kaynak teslimi onun eksik W1 crash/retry kabulünü kapatmaz. M0 toplamı hâlâ
**11 geçti / 7 kısmi / 4 açık**.
