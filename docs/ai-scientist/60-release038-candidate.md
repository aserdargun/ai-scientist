# 0.38.0 sürümü

2026-09-30: çalışan ana sürüm **0.38.0**. Gerçek dağıtım root session
**22136 / exit 0**, 6,821 saniye; 14 dosya ve 0032 migration uygulandı.

Aday; gerçek CARE kalibrasyonunun development veri bağını, sınırlı yetkili
holdout kayıt kilidini, okurken dizin oluşturmayan artifact erişimini,
kayıtlardan izin/kanıt kontrollü SFT dışa aktarmayı ve kullanıcı systemd
oturum bilgilerini konsol başlatıcısına aktarmayı içerir.

## İcra edilmiş kontroller

- Birleşik kalite kapısı: **1104 passed / 7 skipped / 120 deselected**,
  yedi komut exit 0; root session **26831 / exit 0**. 300 gate dosyası
  kontrol boyunca değişmedi.
- İmaj build ve byte eşliği: root session **8871 / exit 0**, build ve
  parity exit 0; çalışma kullanıcısı `10001:10001`.
- İlk imaj denemesi offline bağımlılık katmanı cache eşleşmesi nedeniyle
  başarısız oldu. Ayrı denemede yalnız kopyalanmış build context izinleri
  düzeltildi; kaynak byte'ları korunarak güvenilir offline cache kullanıldı.
- İlk gate, uzun Unix socket yolu ve test kopyasında eksik `.venv` nedeniyle
  başarısız oldu. Ayrı denemede kısa, özel TMPDIR ve mevcut araç ortamına
  bağlantı kullanıldı. İlk başarısız kayıtlar korunur.

[Kaynak hashleri ve gerçek exit code bağı](review-evidence/release038-candidate-binding.json).
İmaj üretimi host Docker daemon'ındadır; doğrulayıcı cgroup sınırı Docker
build daemon'ını sınırladığı iddiası taşımaz.

## Gerçek geçiş ve geri dönüş kanıtı

Ayrı PostgreSQL kopyasında 0031 → 0032 ve özgün katalog tanımlarıyla
0032 → 0031 başarıyla ölçüldü: 47 tablo, 234.073 satır, 54 bütçe checkpoint
ve 7 deadline/nesil satırı korundu. Owner/ACL, roller, trigger'lar ve aynı
kopyadaki işlev OID'leri eşleşti. Ana ledger bu prova boyunca değişmedi.

İlk canlı dağıtım SQL sürücüsünün `%ROWTYPE` yer tutucu yorumlaması
nedeniyle exit 1 verdi; tam kaynak/0031/ledger geri dönüşü doğrulandı.
Ayrı salt okunur test ham SQL hatasını yeniden üretti ve derlenmiş SQL
yolunu exit 0 ile doğruladı. İkinci dağıtım yalnız çalıştırma biçimi
düzeltilerek exit 0 verdi. İlk hatalı sürücü ve receipt korunur.

[Bağımsız dağıtım ve clone özeti](review-evidence/release038-deployment-summary.json).

Yalnız üç sahipli CPU servisi yenilendi; tünel kimliği korundu. Migration
ikisi de katalog hash/CAS kontrollü iki trigger işlevini değiştirdi; mevcut
veri, bütçe ve deadline satırları aynı kaldı. Ana veritabanına tam yedek
geri yüklemesi yapılmadı. AOS/GPU çalışma durumu bu teslimatla değiştirilmedi.

Bu CPU kapısı gerçek yerel LLM, QLoRA, AOS ile GPU paylaşımı veya
araştırma/holdout kabulünü kapatmaz. SFT exporter'ın mevcut gerçek kayıtlarda
12 strict belge çifti için sonucu sıfır uygun eğitim örneğidir; eğitim
koşusu veya hazır LoRA veri kümesi olarak sunulmaz.
