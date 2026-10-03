# İşçi payload'ı ile özgün istek zarfının ayrı doğrulanması

2026-10-01; Scientist tabanı `abaaef6633414aa58e8da73d38980ed4c059f2bf`.

Gerçek `SystemdAOSProfileRuntime._prepare()` yalnız modelin okuyacağı canonical
payload'ı `request.json` dosyasına yazar ve child kaydına bunun SHA256'sını koyar.
Özgün broker zarfının SHA256'sı ise output binding/result/control/scheduler
kayıtlarında korunur. Önceki `_expected_output_generation()` bu farklı hash'leri
eşit sayıyordu. Bu yüzden gerçek worker sonucu persistence'da reddedilebiliyordu;
elle oluşturulmuş test child kayıtları yanlışlıkla zarf hash'i kullanıyordu.

Yeni regresyon, gerçek `_prepare()` fonksiyonunun ürettiği dosyayı ve child
kaydını kullanır. Model artefakt doğrulaması fixture'dır; model/süreç başlatılmaz,
unit generation gözlemleri sentetiktir. Üretim kodu değiştirilmeden geçerli
fixture ile **2 failed / 1 passed**: doğru payload reddi ve child hash'ini
zarf hash'iyle değiştiren saldırının kabulü. İlk fixture cgroup kurulum hatasının
ayrı başarısız çıktısı da korunur; geçerli RED onun ardından çalıştırıldı.

Düzeltme tam zarfın immutable bytes/hash/profile/schema/principal kontrolünü
korur. Bu doğrulanmış zarfın payload'ı role-specific `_profile_payload()` ile
doğrulanıp canonicalize edilir; child hash'i buna bağlanır. Owner/allocation
fence, profile/deployment, boot/start_ticks ve bağımsız worker generation
kontrolleri korunur. Özgün veriler yeniden hash'lenerek DB'ye yazılmaz; mevcut
yanlış child kayıtları otomatik benimsenmez.

Üç regresyon geçti. Profile-output/persistence/replay/terminal publication ve
executor kontrolleriyle birleşik **29 passed / parent exit0,3,699 saniye**.
Elle hazırlanmış iki test fixture'ı producer'ın payload hash'ine uyarlandı;
zarf hash'i kayıtları değiştirilmedi.

Bu kaynak/CPU icrası gerçek model, fiziksel GPU cleanup veya AOS kabulü değildir.
VRAM/model/quantization/context/GPU wait/devir gecikmesi ölçülmedi. Canlı servis,
AOS checkout'u ve canonical scheduler değiştirilmedi. Push/merge/deploy yok.
Authenticated source/physical provider ve tek canonical broker rezervasyonuyla
gerçek bounded model/araştırma/bağımsız Scorer/AOS rapor koşusu hâlâ gereklidir.
Kabul toplamı11 geçti/7 kısmi/4 açık; lisans ve genel CI işleri ayrı kalır.

[İcra ve hash kaydı](review-evidence/worker-payload-envelope-binding.json)

Zorunlu kalite kapısı: **1808 passed / 7 opt-in skipped /120 GPU-live
deselected**, yedi exit0; parent149,832675523 saniye, Python kaynakları sabit.
