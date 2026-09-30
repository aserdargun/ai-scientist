# Sandbox syscall gözlemcisi — 0.37.0 teslimatı

## Doğrulanmış davranış

Aday fit/score çalışmasında syscall gözlemcisi, adayın kendi raporuna bağlı
kalmadan ağ ve süreç sinyali girişimlerini izler. Docker varsayılan seccomp
koruması korunur. NumPy/BLAS ve normal Python thread kullanımı desteklenir.
Sabit polling beklemesi yerine SIGCHLD bildirimi ve sınırlı bekleme kullanılır;
çağıranın sinyal maskesi çıkışta geri yüklenir.

Tam kalite kapısı: **1059 passed / 7 skipped / 117 deselected**, yedi komut
exit 0. Pinli imajda 144 runtime dosyası birebir eşleşir. Gerçek sandbox
kontrolünde beş testin 15 setup/call/teardown aşaması geçti; altı konteynerin
varsayılan seccomp ve izolasyon ayarları doğrulandı. Sayısal fit/score 7,93
saniyede, thread örneği 1,18 saniyede tamamlandı.

İlk gerçek test başlatıcısının **exit 1** sonucu korunur: Docker'ın olmayan
konteyner için stdout'a tek newline yazmasını cleanup kontrolü yanlış yorumladı.
Test süreci exit 0 verdi. Ayrı, sınırlı doğrulayıcı **exit 0** ile aynı altı
konteynerin tam ID'lerinin silindiğini ve kaynak/imaj hash'lerinin korunduğunu
kanıtladı. Bu ek kanıt önceki başlatıcı sonucunu değiştirmez.

## Kaynak ve icra kanıtları

- [Tam kalite kapısı](review-evidence/observer0370-gate-4dfa4ee66420.json)
- [Yedi komut](review-evidence/observer0370-gate-4dfa4ee66420-commands.json)
- [İmaj eşliği](review-evidence/observer0370-observer-integration-053-image-8fe98fcc6912.json)
- [Özgün test sonucu](review-evidence/observer0370-receipt.json)
- [Özgün başlatıcı sonucu](review-evidence/observer0370-probe-launch-r5.json)
- [Ayrı cleanup doğrulaması](review-evidence/observer0370-probe-r5-strict-supplement.json)
- [Dağıtılacak on dosya](review-evidence/observer0370-promotion-manifest.json)

## Kurulum durumu

Ana kurulum **0.37.0** kullanıyor. Gerçek dağıtım **session 22962 / exit 0**,
2,859 saniye ve 79,8 MiB tepe bellekle tamamlandı. On dosya kaynak
hashleriyle doğrulandı; yalnız üç sahipli Lab CPU servisi yeniden başlatıldı.
SQL revision 0031, tüm seçili ledger kayıtları ve bütçe checkpoint hashleri
korundu. Tünelin süreç kimliği değişmedi. Özel kaynak/DB yedekleri tutuldu.
[Dağıtım sonucu](review-evidence/observer0370-deployment.json),
[icra bağı](review-evidence/observer0370-release-execution-binding.json).
Yayımlanan kopyadan özel DSN dosyası hashleri çıkarıldı; özgün kanıt özel
alanda korunur. SQL migration uygulanmadı. Bu teslimat tüm güvenlik, gerçek model/GPU, public
araştırma, holdout veya AOS birlikte çalışma kabullerini tek başına kapatmaz.

## 2026-09-30 ek gerçek izolasyon kanıtı

**Session 34318 / exit 0**, beş test / 15 aşama ve altı konteyner.
DNS resolver, UDP/53 ve HTTP girişimleri, aday hata yakalayıp normal çıktı
üretmeye çalışsa da `forbidden_access` ile reddedildi. Typed fit yalnız
64 train satırı gördü; ayrı score konteyneri 16 eval satırını aldı. Host label/
eval dosyaları, frame attrs ve özel index aktarılmadı. Mount, cihaz, ağ,
CPU/RAM/PID ve salt okunur erişim ayarları gerçek inspect ile doğrulandı.
Gerçek sandbox çıktısındaki tool çağrısı Scorer parser tarafından reddedildi;
host canary değişmedi. Bu üretim parser sınırıdır; gerçek LLM çağrısı yapılmadı.

Özgün R1 **exit 1** korunur: score test aracı zorunlu fit_artifact argümanını
vermemişti. Düzeltme yalnız bu argümanı sağladı. R2 testleri 11,62 saniyede;
unit 12,941 saniye, 194 MiB tepe bellek ve sıfır swap ile tamamlandı.
Altı tam konteyner ID'si silindi; private intent yok, kaynak hashleri korundu.

[Kaynak bağlı özet](review-evidence/sandbox058-summary.json),
[R2 sonucu](review-evidence/sandbox058-r2-receipt.json),
[korunan R1 sonucu](review-evidence/sandbox058-r1-receipt.json).
Eski 19 guard ve crash-window vakaları bu dar ölçümle yeniden doğrulanmış
sayılmaz; M0.3 bütünü açık kalan alt kanıtlarla kısmi durumdadır.
