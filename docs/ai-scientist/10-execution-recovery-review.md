# Kalıcı yürütme ve toparlanma incelemesi

Tarih: 2026-09-24. Önceki guard dilimi `602aecd`, kanıt açıklamaları `1da9a7f` ile kaydedildi. Bu inceleme, Director'ın 20 deneylik koşusuna giderken yarım yazımların ve skorsuz görev sonuçlarının doğru ele alınmasını kapsar. Tam Director ve AOS/GPU birlikte çalışma kabulü değildir.

## Blob yazıcısının gerçek süreç kaybı

[Önceki davranış](review-evidence/blob-writer-crash-before.json), yalnız incelemeye ait süreçte dosya `fsync` sonrasında SIGKILL uygulanarak yeniden üretildi. Özel geçici blob kökünde 81 baytlık `.<digest>.<nonce>.tmp` kaldı. Sonraki farklı geçerli skor dokümanının yazımı, kota taramasındaki beklenmeyen dosya kontrolü nedeniyle reddedildi. Canlı blob kökü, AOS veya PostgreSQL değiştirilmedi.

Luna'nın düzeltmesi kota kilidi altında yalnız doğru shard'daki tam digest/nonce biçimine uyan, özel izinli, tek bağlantılı ve mevcut kullanıcıya ait normal geçici dosyaları temizler. Tamamlanmış blob'lar silinmez; dosya sayısı/boyutu sınırlıdır. İlgisiz veya güvensiz girdiler otomatik silinmez.

[Düzeltme kanıtı](review-evidence/blob-writer-crash-recovery-review.json) ve [yeniden üretim script'i](review-evidence/review_blob_crash_recovery.py) gerçek SIGKILL ile 10/10 kontrolü geçti:

- Fsync edilmiş bir geçici dosyanın kaldığı doğrulandı.
- Yeni geçerli yazım başarılı oldu; yalnız bu geçici dosya kaldırıldı.
- Önceden tamamlanmış blob ve kökün yanındaki sentinel değişmedi.
- Yeni blob'un hash'i ve baytları doğru; tekrar yazım idempotent.
- Özel inceleme dizini temizlendi; kaynak hash'i koşu boyunca sabit kaldı.

Bu kontrol host güç kaybı veya dosya sisteminin donanımsal dayanıklılık testi değildir. Hatalı içerik ve DB'ye bağlanmamış tamamlanmış blob'ların yaşam döngüsü ayrıca değerlendirilir.

## Skorsuz görevlerin kapanış sözleşmesi

Önceki plan kapanışı yalnız gerçek Scorer skorlarını kabul ediyordu. Guard reddi, aday çökmesi, timeout veya kullanıcı iptali için sıfır/dummy skor yazmak Referee istatistiğini bozar. `0008_terminal_task_outcomes` migration'ı ve servis değişiklikleri şu sözleşmeyi uygular:

1. Gerçek metrikler `task_scores` içinde kalır. Skorsuz terminal sonuçlar ayrı, sınırlı ve tipli kayıtlardır.
2. Aynı görev kimliği hem skor hem terminal sonuç alamaz. Ortak `task_completions` birincil anahtarı iki yolun aynı çözümleme hakkı için yarışmasını sağlar; sonuç ve bu kayıt tek transaction'da yazılır.
3. Üretici kimliği DB oturumundan türetilir. Scorer'ın terminal yazımı da güncel claim token/lease ile sınırlandırılır; eski worker yeni sahibin görevini kapatamaz.
4. Kuyruk durumu ve sonuç birlikte kapanır. Retry edilebilir altyapı hatası adayın öğrenme etiketine dönüşmez.
5. Stop, yeni görev eklemeyi engeller; mevcut plan kapatılıp iptal sonuçları kaydedilebilir. Henüz görev planlanmamış koşu da sahte görev üretmeden doğrulanmış `stopped` sonucuna ulaşabilmelidir.
6. Finalizer, kapatılmış plan ile skor/terminal sonuçlarının tam ve ayrık kapsamını doğrular. `stopped`/`failed` araştırma sonucu, başarılı araştırma diye gösterilmez; API durum, sahiplik ve rapor hash'ini doğrulayarak sonucu döndürür.

Salt satır kilidi ve iki tabloda ayrı varlık kontrolü, eski snapshot kullanan transaction için yeterli kabul edilmez. [PostgreSQL 16 tutarlılık açıklaması](https://www.postgresql.org/docs/16/applevel-consistency.html), Repeatable Read altında kilitten önce alınmış snapshot'ın sonradan commit edilen değişiklikleri göremeyebileceğini açıklar. Bu tasarımda ortak benzersiz anahtar kullanılır ve yazımlar READ COMMITTED ile sınırlandırılır. Repeatable Read yazımı açıkça reddedilir; bu seviyede iş akışı desteği iddia edilmez.

## Gerçek PostgreSQL ve API kanıtı

[`review_terminal_outcomes.py`](review-evidence/review_terminal_outcomes.py) ayrı Planner, Scorer ve Director kimlikleriyle [36/36 kontrolü](review-evidence/terminal-outcomes-review.json) geçti. Yalnız incelemeye ait UUID koşuları, 16 satırlık sentetik etiketler ve geçici blob dizini kullanıldı; tamamı temizlendi. Kaynak hash'leri koşu boyunca sabit kaldı.

- İki yönde score/terminal yarışı: ilk transaction sonucu yazıp commit etmeden ikinci gerçek DB oturumu başlatıldı. `pg_blocking_pids` ile kilit beklemesi görüldü; ilk commit sonrasında ikinci yazım reddedildi. Her görev için tek ortak completion kaydı kaldı.
- DB oturumu gerçek üreticiyi belirledi; çağrıda verilen sahte üretici kullanılmadı. Yanlış aday, bilinmeyen görev, yetkisiz Director yazımı ve Planner'ın altyapı hatası üretmesi reddedildi. Sonuçlar ilgili uygulama rollerine karşı değişmez kaldı.
- Gerçek kuyruk satırı atlanarak iptal yazılamadı. Doğru queued iş iptali, görev sonucu ile aynı transaction'da kapandı. Çözülmüş göreve yeni iş alınmadı; süresi geçmiş/eski claim yeni sahibin işini kapatamadı. Token sonuç tablosuna kaydedilmedi.
- Açık plan yayımlanmadı. Bir gerçek metrik + bir açık guard reddi içeren rapor, stopped görev raporu, altyapı hatalı failed raporu ve sıfır görevli erken stop doğrulandı. Sahte metrik eklenmedi.
- Gerçek Director DB rolüne bağlı ASGI API, stopped/failed raporlarını doğrulanmış hash ile döndürdü; yetkisiz istek, yanlış sahip, durum uyuşmazlığı ve değiştirilmiş rapor içeriği reddedildi. Bu kontrol ağ dinleyicisi veya AOS istemcisi çalıştırmadı.

Migration öncesinde ayrıca gerçek Scorer ile bir skor hazırlandı. [Yükseltme kontrolü](review-evidence/terminal-legacy-upgrade-review.json) 3/3 geçti: eski skor değişmedi, ortak completion kaydı dolduruldu ve karşı terminal sonuç reddedildi. Yalnız bu eski fixture'ın UUID ve veri seti silindi. İlk birleşik inceleme script'i daha sonraki rapor kontrolünde yanlış anahtar kullandığı için durdu; üretim kodu değiştirilmeden inceleme script'i düzeltildi ve yukarıdaki 36 kontrol tam koşuldu.

Şema incelemesinde migration'ın eklediği `score_job_id` foreign key'inin Python metadata'sında eksik olduğu görüldü (`alembic check`, exit 255). Metadata eşleştirildi; [son karşılaştırma](review-evidence/terminal-schema-parity.json) exit 0 ve `No new upgrade operations detected` sonucunu verdi. Bu salt okunur kontrolde migration veya veri değiştirilmedi.

## Aday çıktısı ve altyapı hatası ayrımı

Son incelemede ayrıştırıcının `1e999`/`-1e999` gibi JSON sayılarının float sonsuza taşmasını ve indeks/skor dizilerinin farklı uzunlukta olmasını kabul ettiği görüldü. Sabit `602aecd` kaynak sürümünde [yeniden üretim](review-evidence/candidate-score-parser-before.json), üç bozuk çıktının kabul edildiğini ve temiz kontrolün geçtiğini gösterir. Genel VUS `ValueError` hatasını altyapı hatası olarak ele alan yeni worker yolu açısından bu sınıflandırma yanlıştır.

Luna'nın düzeltmesi sonlu skorları, eşit dizi uzunluklarını ve artan benzersiz indeksleri doğrular. Bu doğrulama hataları `CandidateOutputError` olur. [Bağımsız parser kontrolü](review-evidence/candidate-score-parser-fixed.json) 4/4 geçti: iki taşma ve uzunluk hatası tipli aday hatasıyla reddedildi; temiz kontrol kabul edildi. Script'in `before` modu eşzamanlı düzenlenen dosya yerine sabit Git sürümünü okur.

## Ayrı worker süreçlerinde hata kapanışı

[`review_terminal_worker.py`](review-evidence/review_terminal_worker.py) altı gerçek `lab.scorer.worker` süreciyle [10/10 kontrolü](review-evidence/terminal-worker-review.json) geçti. İnceleme launcher'ı özel blob kökünü seçti; süreçler üretimin ortak Scorer slice'ında, 2 GiB RAM/swap 0, bir CPU, 32 PID ve 30 saniye sınırıyla çalıştı. Bu, üretim launcher'ının bütün yaşam döngüsüne ilişkin bir kanıt değildir.

- Özel fixture'ın blob'u silindi. İlk iki girişim terminal sonuç yazmadan kuyruğa döndü; üçüncü girişim `scorer_error` sonucu ve `failed` iş durumu üretti.
- Gerçek etiket dizisinden kısa, biçim olarak geçerli aday çıktısı ilk girişimde `candidate_rejected` oldu; altyapı yeniden denemesine dönüşmedi.
- Ayrı finalizer, işlemin `finalized` oluşunu raporun `failed`/`completed` araştırma durumundan ayırdı. Aday reddiyle kapanan `completed` koşu, KEEP veya başarılı aday bulma kanıtı değildir.
- İki hata yolu da metrik skoru yazmadı. Altı unit'in inactive/failed ve MainPID=0 olduğu görüldü; özel artifact dizini ve DB kayıtları temizlendi. Kaynak hash'leri sabit kaldı.

## Açık yürütme işleri

Bu kanıtlar, DB transaction, API ve belirtilen hata yollarını doğrular. Aktif worker/cgroup'un gerçekten boşaltıldığının kanıtı, sahibi kaybolmuş `stop_requested` claim'in güvenli otomatik kapanışı, tekrar başlatmada terminal yazımların idempotent devamı ve Director'ın 20 deneylik tam akışı bu incelemeyle tamamlanmış sayılmaz. Tam Referee, public veri, yerel model ve adil AOS/GPU ilerleme kabulleri ayrıca açıktır.
