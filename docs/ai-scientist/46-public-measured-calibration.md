# Gerçek veri kalibrasyonu ve kayıt kurtarması

Tarih: 2026-09-30. Kapsam: ticari olmayan araştırma; izole PostgreSQL,
gerçek CARE Farm B verisi, üretim Scorer ve Docker ölçümleri.

## Ölçülen sonuç

15 görev × 3 algoritma × 3 seed = **135 gerçek hücre** tamamlandı.
Algoritmalar `robust_z`, `iforest`, `ecod_train_frozen`; seed'ler 0, 1, 2.
Salt okunur kontrol 135 ayrı başarılı claim, sıfır başarısız claim,
sonlu/sınırlı skorlar ve 15 görev özeti gösteriyor.
Bu ilk salt okunur ölçüm kontrolü tek başına trusted binding veya bağımsız
proof değildi. Sonraki ayrı kayıt kurtarması ve özgün proof aşağıda belirtilir.

Onarılan v2 attempt'in rezervasyonu 13.500/13.733 saniye; deadline
`2026-09-30T01:00:05.999077Z`. Özgün v1 attempt eksik kaldı;
100 saniye ücreti ve `2026-09-30T01:00:16.500812Z` deadline'ı korunuyor.
Kaynak değişikliği eski koşunun devamı olarak sunulmadı.

Ölçüm kaynak kimliği:

- Harness: `a390f4a1838657d738bf590510f38a699b31fff663a6ece057c873b154fdae19`.
- Image: `sha256:3f915d48f39042e2261f218a9151981302d8d2cf29f983f2b1c73d5a183fb62f`.
- Kaynak envanteri: `2fe121c86a2d9586545e00553c38eb137901c9471d92e41b287d219193ffa83a`.

## Özgün kayıt aşamasının korunmuş hatası

Ölçümden sonraki holdout suite kaydı `ProgrammingError` ile başarısız oldu.
Launcher **exit 1**; unit journal son durumunda **exit 120** kaydı var.
Geçici systemd unit sonradan inactive/success gösterdi; özgün hata receipt'i
sonuç için esas alınır. Tamamlanmış hücreler yeniden çalıştırılmadı.

Salt okunur transaction içinde yalnız `EXPLAIN` ile SQLSTATE `42501`
yeniden üretildi: kayıt sorgusundaki `SELECT ... FOR UPDATE`, Scorer'ın
SELECT/INSERT yetkisi bulunan değişmez tablo için UPDATE yetkisi istiyor.
Özgün başarısız işlem suite version kaydetmedi. Sonraki kayıt kurtarması
ayrı registrar kaynak kimliğiyle yapıldı; ölçüm kaynağı, deadline ve hücreler
değiştirilmedi. Özgün launcher hatası başarılı sonuç olarak yeniden yazılmadı.

## Ayrı kayıt kurtarması ve bağımsız proof

2026-09-30 tarihli registrar062 kurtarması, özgün 056 ölçüm kaynağını ve
veritabanı kimliğini koruyarak measured suite v2 ve public suite v3 kayıtlarını
tamamladı. Yeni ölçüm/worker dispatch yapılmadı; ölçüm/image pinleri değişmedi.
Her iki attempt'in tüm sütunlarını kapsayan önce/sonra tablo digest'leri ve
feature artefakt hash özeti eşleşti. Bu inceleme receipt'in toplu koruma
digest'ini bağımsız yeniden hesapladı; registrar kodu ve release dosyasının
hash'lerini özgün kurtarma receipt'ine karşı doğruladı.

Özgün `proof056.py` **session 58086 / exit 0** ile salt okunur proof üretti.
135 gerçek hücrenin tam worker generation bağı, her görevde üç seed üzerinden
algoritma ortalamaları, baseline/reference ve ağırlık hesapları ile iki trusted
suite kaydı kontrol edildi. Özel proof SHA256'sı
`74264d73236b2a1bcb46b0f0ede8628f17761480303fcaaeb7685345b86ad970`.
Bu dokümantasyon incelemesi özgün helper/proof hash'lerini, kalibrasyon bağını
ve özel receipt'teki özet/ağırlıklı toplam aritmetiğini ayrıca doğruladı;
veritabanını yeniden sorgulamadı.

Yayımlanan özetler yalnız hash, kontrol sonucu ve icra bağı taşır. Farm B'nin
sayısal skorları, görev kimlikleri, özel veritabanı kimliği ve bağlantı
bilgileri yayımlanmadı. Kayıt/proof'un tamamlanması gerçek public araştırma
ve araştırma sonu bağımsız holdout kabulünü kapatmaz: **M0.10 OPEN**.

## Kanıt ve açık kapsam

- [135 hücrenin salt okunur kontrolü](review-evidence/public056-measured135-readonly.json).
- [Özgün başarısız launcher](review-evidence/public056-calibrate-repair-execution.json).
- [Salt okunur hata teşhisi](review-evidence/public056-registration-readonly-diagnosis.json).
- [Kayıt kurtarmasının hash özeti](review-evidence/public056-registration-recovery-summary.json).
- [Özgün actual135 proof'un hash özeti](review-evidence/public056-actual135-proof-summary.json).
- [Arayüzde gösterilen tarihli durum](review-evidence/public056-calibration-progress.json).
- [Kurtarma öncesindeki durum](review-evidence/public056-calibration-progress-before-recovery.json).

Trusted binding ve üç-seed ortalamaları/ağırlık proof'u artık ayrı kanıtlarla
tamamlandı. Gerçek public araştırma ve koşu sonu holdout henüz kabul edilmedi.
Bu koşu gerçek LLM çağrısı, GPU/AOS birlikte çalışma veya eğitim kanıtı değildir.
