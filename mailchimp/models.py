import json

from django.contrib.contenttypes.models import ContentType
from django.urls  import reverse
from django.db import models
from django.utils.translation import ugettext_lazy as _

try:
    from django.contrib.contenttypes.fields import GenericForeignKey
except ImportError:
    from django.contrib.contenttypes.generic import GenericForeignKey

from .managers import CampaignManager, QueueManager


class Queue(models.Model):
    """
    A FIFO queue for async sending of campaigns
    """
    campaign_type = models.CharField(max_length=50)
    contents = models.TextField()
    list_id = models.CharField(max_length=50)
    template_id = models.PositiveIntegerField()
    subject = models.CharField(max_length=255)
    from_email = models.EmailField(max_length=254)
    from_name = models.CharField(max_length=255)
    to_email = models.EmailField(max_length=254)
    folder_id = models.CharField(max_length=50, null=True, blank=True)
    tracking_opens = models.BooleanField(default=True)
    tracking_html_clicks = models.BooleanField(default=True)
    tracking_text_clicks = models.BooleanField(default=False)
    title = models.CharField(max_length=255, null=True, blank=True)
    authenticate = models.BooleanField(default=False)
    google_analytics = models.CharField(max_length=100, blank=True, null=True)
    auto_footer = models.BooleanField(default=False)
    generate_text = models.BooleanField(default=False)
    auto_tweet = models.BooleanField(default=False)
    segment_options = models.BooleanField(default=False)
    segment_options_all = models.BooleanField(default=False)
    segment_options_conditions = models.TextField()
    type_opts = models.TextField()
    content_type = models.ForeignKey(ContentType, null=True, on_delete=models.DO_NOTHING, blank=True,)
    object_id = models.PositiveIntegerField(null=True, blank=True)
    content_object = GenericForeignKey('content_type', 'object_id')
    extra_info = models.TextField(null=True)
    locked = models.BooleanField(default=False)

    objects = QueueManager()

    def send(self):
        """
        send (schedule) this queued object
        """
        from .utils import get_connection

        # check lock
        if self.locked:
            return False
        # aquire lock
        self.locked = True
        self.save()

        # get connection and send the mails
        connection = get_connection()
        template = connection.get_template_by_id(self.template_id)

        content_data = dict([(str(k), v) for k, v in json.loads(self.contents).items()])
        built_template = template.build(**content_data)

        tracking = {
            'opens': self.tracking_opens,
            'html_clicks': self.tracking_html_clicks,
            'text_clicks': self.tracking_text_clicks,
        }

        if self.google_analytics:
            analytics = {'google_analytics': self.google_analytics}
        else:
            analytics = {}

        segment_opts = {
            'match': 'all' if self.segment_options_all else 'any',
            'conditions': json.loads(self.segment_options_conditions),
        }

        type_opts = json.loads(self.type_opts)
        title = self.title or self.subject

        _list = connection.get_list_by_id(self.list_id)

        camp = connection.create_campaign(
            campaign_type=self.campaign_type,
            campaign_list=_list,
            template=built_template,
            subject=self.subject,
            reply_to=self.from_email,
            from_name=self.from_name,
            to_name=self.to_email,
            folder_id=self.folder_id,
            tracking=tracking,
            title=title,
            authenticate=self.authenticate,
            analytics=analytics,
            auto_footer=self.auto_footer,
            auto_tweet=self.auto_tweet,
            segment_opts=segment_opts,
            rss_opts=type_opts,
        )

        if camp.send_now_async():
            self.delete()
            kwargs = {}
            if self.content_type and self.object_id:
                kwargs['content_type'] = self.content_type
                kwargs['object_id'] = self.object_id
            if self.extra_info:
                kwargs['extra_info'] = json.loads(self.extra_info)
            return Campaign.objects.create(camp.id, segment_opts, **kwargs)
        # release lock if failed
        self.locked = False
        self.save()
        return False

    def get_dequeue_url(self):
        return reverse('mailchimp_dequeue', kwargs={'id': self.id})

    def get_cancel_url(self):
        return reverse('mailchimp_cancel', kwargs={'id': self.id})

    def get_list(self):
        from .utils import get_connection

        return get_connection().lists[self.list_id]

    @property
    def object(self):
        """
        The object might have vanished until now, so triple check that it's there!
        """
        if self.object_id:
            model = self.content_type.model_class()
            try:
                return model.objects.get(id=self.object_id)
            except model.DoesNotExist:
                return None
        return None

    def get_object_admin_url(self):
        if not self.object:
            return ''
        name = 'admin:%s_%s_change' % (self.object._meta.app_label,
            self.object._meta.model_name)
        return reverse(name, args=(self.object.pk,))

    def can_dequeue(self, user):
        if user.is_superuser:
            return True

        if not user.is_staff:
            return False

        if callable(getattr(self.object, 'mailchimp_can_dequeue', None)):
            return self.object.mailchimp_can_dequeue(user)
        return user.has_perm('mailchimp.can_send') and user.has_perm('mailchimp.can_dequeue')


class DeletedCampaign(object):
    subject = u'<deleted from mailchimp>'


class Campaign(models.Model):
    sent_date = models.DateTimeField(auto_now_add=True)
    campaign_id = models.CharField(max_length=50)
    content = models.TextField()
    name = models.CharField(max_length=255)
    content_type = models.ForeignKey(ContentType, null=True, on_delete=models.DO_NOTHING, blank=True)
    object_id = models.PositiveIntegerField(null=True, blank=True)
    content_object = GenericForeignKey('content_type', 'object_id')
    extra_info = models.TextField(null=True)

    objects = CampaignManager()

    class Meta:
        ordering = ['-sent_date']
        permissions = [('can_view', 'Can view Mailchimp information'),
                       ('can_send', 'Can send Mailchimp newsletters')]
        verbose_name = _('Mailchimp Log')
        verbose_name_plural = _('Mailchimp Logs')

    def get_absolute_url(self):
        return reverse('mailchimp_campaign_info', kwargs={'campaign_id': self.campaign_id})

    def get_object_admin_url(self):
        if not self.object:
            return ''
        name = 'admin:%s_%s_change' % (self.object._meta.app_label,
            self.object._meta.model_name)
        return reverse(name, args=(self.object.pk,))

    def get_extra_info(self):
        if self.extra_info:
            return json.loads(self.extra_info)
        return []

    @property
    def object(self):
        """
        The object might have vanished until now, so triple check that it's there!
        """
        if self.object_id:
            model = self.content_type.model_class()
            try:
                return model.objects.get(id=self.object_id)
            except model.DoesNotExist:
                return None
        return None

    @property
    def mc(self):
        from .utils import get_connection

        try:
            if not hasattr(self, '_mc'):
                self._mc = get_connection().get_campaign_by_id(self.campaign_id)
            return self._mc
        except:
            return DeletedCampaign()


class Reciever(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.DO_NOTHING, related_name='receivers')
    email = models.EmailField(max_length=254)
