# Ortak GPU kuyruk yolu

0.27.0, Lab CLI'nin broker ile aynı SQLite kuyruğunu kullanmasını sağlar.
İzinli yollar, önceki özel test dizininin doğrudan altındaki dosya ve
tek production yolu `~/.local/state/swapp-gpu/arbiter.sqlite3` ile sınırlıdır.
API isteği dosya yolu seçemez. Orijinal yolun tüm dizin bileşenlerinde
symlink/sahiplik/izin kontrolleri yapılır; mevcut DB normal, özel ve
kullanıcıya ait dosya olmalıdır. Sabit broker yolu ve soketi değişmedi.

Lab yerel modeli kendi süreç kimliğiyle aynı scheduler'a doğrudan katılır.
AOS modelleri kimlik doğrulamalı broker üzerinden katılır. Broker'ın Lab
kaydını yeniden doğrulaması 0.26 canonical Director yamasıyla sağlanır.
Birlikte çalışma sürücüsü iki tarafın gerçekten aynı DB ve güncel servis
kimliğini kullandığını çalıştırmadan önce doğrulamalıdır.

Luna'nın ayrı kopyasında 10 hedefli test/Ruff/mypy/py_compile exit 0.
İlk kaynak yaması yeni test dosyasını içermiyordu; hata root uygulamasında
tespit edildi. İlk yama/kanıt değiştirilmedi, ayrı test yaması hazırlandı.
İki yamanın temiz başlangıca sırayla uygulanması ve ana koddaki tam byte
eşliği doğrulandı. `lab-shared-runtime-db-026-test-supplement.json`
bu düzeltmeyi kaydeder.

## Kalite ve kapsam

İmaj yapımı ve tam kalite **session 90787 / exit 0**:
**334 passed, 7 skipped, 13 deselected**, strict mypy 78 kaynak;
yedi kalite komutu başarılı, 87 runtime dosyasında byte eşliği.
Kaynak/imaj/komut bağı `parallel-integration-027-quality-gate-binding.json`.

Canlı AOS veya GPU servisi çalıştırılmadı/değiştirilmedi. Gerçek S2,
public araştırma, holdout, eğitim ve AOS desktop birlikte kabulü açıktır.
Önceki 0.26 guard/alarm testleri ayrı kaynak bağıyla M0.4–7'yi kapatır:
güncel toplam **7 geçti / 11 kısmi / 4 açık**.
