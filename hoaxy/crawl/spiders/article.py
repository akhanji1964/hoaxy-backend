# -*- coding: utf-8 -*-
"""ArticleParser implementation.

We use webparser from https://mercury.postlight.com/ to parse a URL into
structured article.
"""
#
# written by Chengcheng Shao <sccotte@gmail.com>

import json
import logging

import lxml.html
import scrapy
from scrapy.spidermiddlewares.httperror import HttpError
from sqlalchemy.exc import SQLAlchemyError

from hoaxy.database.models import (
    A_DEFAULT,
    A_P_SUCCESS,
    A_WP_ERROR_DATA_INVALID,
    A_WP_ERROR_DROPPED,
    A_WP_ERROR_NONHTTP,
    Article,
)

logger = logging.getLogger(__name__)

HTML_KEEP_ALL = 0
HTML_KEEP_FAILURE = 1
HTML_KEEP_NONE = 2


class ArticleParserSpider(scrapy.spiders.Spider):
    """A spider for article Web parser.

    The web parser service is provided by https://mercury.postlight.com/.
    This spider build requests, and send them to the service and parse the data
    and insert into our article table.
    """
    name = 'article_parser.spider'

    def __init__(self, session, nt_articles, api_key, *args, **kwargs):
        """Constructor of ArticleParserSpider.

        Parameters
        ----------
        session : obj
            A SQLAlchemy session instance.
        nt_articles : list
            A list of namedtuples, hoaxy.crawling.items.NTArticle
            (id, canonical_url)
        api_key : string
            The API key to use the service.
        """
        self.session = session
        self.nt_articles = nt_articles
        self.api_key = api_key
        self.html_mode = kwargs.pop('html_mode', HTML_KEEP_ALL)
        super(ArticleParserSpider, self).__init__(*args, **kwargs)

    def start_requests(self):
        for nt_article in self.nt_articles:
            url = "https://mercury.postlight.com/parser?url={}".format(
                nt_article.canonical_url.encode('utf-8', errors='ignore'))
            yield scrapy.Request(
                url,
                callback=self.parse_item,
                errback=self.errback_request,
                meta=dict(nt_article=nt_article),
                headers={'x-api-key': self.api_key})

    def errback_request(self, failure):
        """Back call when error of the request.

        This function is called when exceptions are raise by middlewares.
        """
        request = failure.request
        nt_article = request.meta['nt_article']
        logger.error('Fail to do web parsing request for %s',
                     nt_article.canonical_url)
        if failure.check(HttpError):
            # these exceptions come from HttpError spider middleware
            # you can get the non-200 response
            response = failure.value.response
            status_code = response.status
            logger.error('HTTP ERROR %r of web parser %r', status_code,
                         request.url)
        else:
            status_code = A_WP_ERROR_NONHTTP
        try:
            self.session.query(Article).filter_by(id=nt_article.id)\
                .update(dict(status_code=status_code))
            self.session.commit()
        except SQLAlchemyError as e:
            logger.error(e)
            self.session.rollback()

    def parse_item(self, response):
        """Parse the response into an ArticleItem."""
        nt_article = response.meta['nt_article']
        article_data = dict()
        status_code = A_P_SUCCESS
        # load json data
        try:
            data = json.loads(response.text)
        except Exception as e:
            data = None
            logger.warning('Error when loading response from webpaser: %s', e)
        # exceptions of the data
        if data is None or 'title' not in data or len(data['title']) == 0\
                or 'content' not in data or len(data['content']) == 0:
            status_code = A_WP_ERROR_DATA_INVALID
        else:
            # fill item with data
            try:
                article_data['title'] = data['title']
                article_data['content'] = lxml.html.fromstring(
                    html=data['content']).text_content()
                article_data['meta'] = dict(
                    dek=data['dek'],
                    excerpt=data['excerpt'],
                    author=data['author'])
                if data['date_published'] is not None:
                    article_data['date_published'] = data['date_published']
                article_data['status_code'] = A_P_SUCCESS
            except Exception as e:
                logger.error('Error when parsing data from webparser %r: %s',
                             data, e)
                status_code = A_WP_ERROR_DATA_INVALID
        # Update article
        if status_code != A_P_SUCCESS:
            article_data = dict(status_code=status_code)
            # clean html if necessary
            if self.html_mode == HTML_KEEP_NONE:
                article_data['html'] = None
        else:
            # clean html if necessary
            if self.html_mode == HTML_KEEP_FAILURE:
                article_data['html'] = None
        try:
            self.session.query(Article).filter_by(id=nt_article.id)\
                .update(article_data)
            self.session.commit()
        except SQLAlchemyError as e:
            logger.error(e)
            self.session.rollback()

    def close(self, reason):
        """Called when closing this spider.

        When closing spider, if the reason is 'finished' (meaning
        successfully run the crawling process), we set `status_code` of
        these response non-received articles as A_WP_ERROR_NO_SCRAPY_RESPONSE.
        """
        if reason == 'finished':
            logger.warning("""Update unreceived record when closing spider \
with reason='finished""")
            article_data = dict(status_code=A_WP_ERROR_DROPPED)
            if self.html_mode == HTML_KEEP_NONE:
                article_data['html'] = None
            try:
                self.session.query(Article)\
                    .filter_by(status_code=A_DEFAULT)\
                    .filter(Article.id.in_([x.id for x in self.nt_articles])
                            ).update(article_data, synchronize_session=False)
                self.session.commit()
            except SQLAlchemyError as e:
                logger.error(e)
                self.session.rollback()
        logger.warning('%s closed with reason=%r', self.name, reason)
