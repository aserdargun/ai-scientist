# CARE kalibrasyonu: süreç ve bütçe entegrasyonu — 2026-09-27

Ana çalışma sürümü **0.32.0**; son imaj eşliği ve tam kalite kapısı geçti.
Uygulama GPT-6 Luna / high, kaynak incelemesi GPT-6 Astra / high.
Kalibrasyon dosyaları özel çalışma ağacından hash doğrulamasıyla alındı.
Baseline CLI ve Director nesil/restart işleri ayrı ağaçlarda sürüyor.

## Uygulanan sözleşme

- Sabit matris: 15 Farm B görevi × 3 baseline algoritması × seed 0–2,
  toplam 135 hücre. Görevler ve girdi/etiket/semantik/source hash'leri
  ölçüm başlamadan sabitlenir; Planner/Director özel girdileri okuyamaz.
- Migration 0022 özel kalibrasyon kaydı, receipt ve freeze saklamasını;
  0023 kalıcı hücre sahipliğini, bütçe ve süreç nesli kontrollerini ekler.
  Her yeni hücre dış iş başlamadan **100 saniye geri ödenmeyen** bütçe
  ayırır. Hücre ve kalibrasyonun ilk son tarihleri korunur.
- Worker aynı host Scorer P=1 kilidini, ilk son tarihi aşmadan bekler.
  Ağır import ve özel DB işi kilitten sonradır. Başka Scorer'ın geçici
  kullanımı anında tüm kalibrasyonu başarısız saymaz.
- Worker kimliği PID/start/boot/unit/InvocationID/cgroup ile bağlanır.
  Recovery, tam eşleşen süreç ve sahipli sandbox kapanmadan başarısız
  kayıt yazmaz. Terminal receipt bulunsa bile önceki süreç kapanmadan
  sıradaki hücreye geçmez. Belirsizlik pending kalır.
- Tamamlanan hücreler ve kalibrasyon özeti değiştirilemez; aynı tekrar
  mevcut receipt'i döndürür. UUID kimlikleri canonical JSON'da açıkça
  string olarak yazılır ve freeze hash'ine katılır.

## Gerçek PostgreSQL kanıtı ve sınırı

Özel kaynak ağacının 178 dosyası sabitlenerek **yeni ve geçici PostgreSQL 16**
üzerinde çalıştırıldı. Skorlar ve worker kimlikleri sentetiktir; gerçek
candidate/Scorer hücre süreçleri veya Farm B ölçümleri çalıştırılmadı.

| Deneme | Gerçek sonuç |
|---|---|
| 0023 r2 | Session 45914, exit 1; migration geçti, 135 receipt sonrası UUID'nin JSON'a çevrilmemesi nedeniyle freeze başarısız. 5,224 sn; 187,3 M sürücü tepe belleği. |
| 0023 r3 | Session 70379, exit 0; migration 0023 ve **1 test / 102,17 sn** geçti. Toplam 105,387 sn, 1,894 sn CPU, 184 M sürücü tepe belleği. |

R3; 135 bağlı receipt, freeze/yeniden okuma, tek 100 saniyelik borç,
ilk hücre son tarihi geçtikten sonra aynı receipt'in geri okunması,
eksik neslin canlı reserved/running kayıtlar üzerinde reddi, değişmeyen
bütçe/sayaçlar, görev kümesinin mühürlenmesi ve rol retlerini kapsar.
NULL nesil testleri geçerli payload ve uygun canlı durum kullanır;
terminal kayıt veya bozuk payload yüzünden verilen ret kabul edilmez.

Sürücü sınırı 1 GiB / %50 CPU / 64 görev / swap 0; ayrı PostgreSQL
konteyneri 512 MiB / %50 CPU / 64 PID / 256 MiB tmpfs sınırındadır.
Sürücü tepe belleği Docker kardeşini kapsamaz. Her iki denemede de yalnız
sahipli DB konteyneri ve erişim bilgileri temizlendi; AOS ve WinBoat
konteynerleri korundu.

Kanıtlar: [r2 başarısızlık](review-evidence/care-calibration-031-pg-r2-execution-binding.json),
[r3 başarı](review-evidence/care-calibration-031-pg-r3-execution-binding.json),
[Astra R4 incelemesi](review-evidence/care-calibration-031-lifecycle-r4-source-review.json),
[UUID düzeltme incelemesi](review-evidence/care-calibration-031-uuid-source-review.json).
Özel PG kaynağı ile birleşik ana sürümün tüm dosya fingerprint'leri farklıdır;
kanıtlar bu ayrımı korur.

## Birleşik kalite kapısı

İlk ana imajda 108 runtime dosyası eşleşti. İlk tam gate session 63646 /
exit 1: 520 test geçti, 19'u başarısız, 7 atlandı, 23 live/GPU testi
dışarıda; diğer altı komut ve strict mypy 99 kaynak geçti. Tüm başarısızlıklar,
SQLite test şeması oluşturulurken PostgreSQL regex `~` kısıtının
uygulanmasından kaynaklandı. Mevcut `ddl_if(dialect="postgresql")`
ayrımıyla düzeltildi; 34 tablonun PostgreSQL DDL'i byte olarak aynı kaldı.
Başarısız kapı [tam çıktı ve kaynak bağıyla](review-evidence/care-calibration-integration-032-quality-gate-r1-binding.json)
korunur. İkinci gate session 88903 / exit 1'de 539 test ve diğer beş
komut geçti; Ruff dört satır uzunluğunu reddetti. Yalnız formatter uygulanarak
AST ve PostgreSQL DDL eşliği tekrar doğrulandı; bu başarısızlık da
[format incelemesinde](review-evidence/care-calibration-integration-032-format-review.json)
korunur.

Son gate **session 69013 / exit 0**, yedi komut başarılı:
**539 passed / 7 skipped / 23 deselected**, strict mypy 99 kaynak,
Pylint 9,34. 56,929 sn duvar / 52,408 sn CPU / 487,7 M tepe bellek /
swap 0; üst sınır 3 GiB / 2 CPU / 128 görev. 184 kaynak hash'i değişmedi.
Son imaj **session 14611 / exit 0**, 108 runtime dosyası ve 110 snapshot
kaynağı eş; imaj
`sha256:0232ae8625519a9bfdfcc664642f9c1b2f1a7316919763f0b112cfe95ca510ba`.
Harness hash'i
`bfd34a056aea2104e58e8f4e168ea209803fa08a99d1d4931507a7dce642951a`
(86 dosya). Ayrıntılar
[son kalite/kaynak bağı](review-evidence/care-calibration-integration-032-quality-gate-binding.json)
ve [son imaj kaydı](review-evidence/care-calibration-integration-032-image-release.json).

PostgreSQL kanıtının özel ağacından gelen schema dosyasındaki üretim SQL'i
değişmedi; dialect guard ve biçim farkları ayrıca doğrulandı. Gerçek PG
denemesini bütün birleşik ana kaynakta yeniden çalıştırılmış gibi sunmuyoruz.

## Açık kabul

Gerçek 135 hücrenin ölçümü, Farm B kalibrasyonuyla holdout skorlama,
10 KEEP/run-end rollback/canary, gerçek süreç ölümü ve bağımsız drain/CAS
sıra kanıtı açık. Baseline CLI, bütün araştırma restart'ı, gerçek model,
eğitim ve AOS birlikte çalışma kabulü bu SQL testiyle kapanmaz.
Kabul sayısı **10 geçti / 8 kısmi / 4 açık**; PR açılmadı, merge yapılmadı.
